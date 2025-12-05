from flask import Flask, render_template, request, jsonify, send_file, abort
from flask_socketio import SocketIO
from datetime import datetime, timedelta
import sqlite3
import os
import cv2
import requests
import numpy as np
from PIL import Image
from io import BytesIO
from ultralytics import YOLO
import easyocr
import re
import time

# ======================================
# 1. CONFIGURATION
# ======================================
AI_SERVER_IP = "0.0.0.0"  # Nghe trên mọi IP trong mạng LAN
PORT = 5000

# IP Camera (ESP32-CAM)
CAM_ENTRY_IP = "172.20.10.4"
CAM_EXIT_IP  = "172.20.10.5"

# Đường dẫn file
CAPTURE_FOLDER = "static/captures"
DB_FILE = "database.db"
YOLO_MODEL_PATH = "models/best.pt"

# Tạo thư mục lưu ảnh
os.makedirs(CAPTURE_FOLDER, exist_ok=True)

# Khởi tạo Flask & SocketIO
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*")

# ======================================
# 2. LOAD AI MODELS
# ======================================
print("--- LOADING AI MODELS ---")
model = None
try:
    if os.path.exists(YOLO_MODEL_PATH):
        print(f"[YOLO] Loading custom model: {YOLO_MODEL_PATH}")
        model = YOLO(YOLO_MODEL_PATH)
    else:
        print("[YOLO] Custom model not found, loading standard yolov8n.pt...")
        model = YOLO("yolov8n.pt")
except Exception as e:
    print(f"[YOLO ERROR] {e}")

reader = None
try:
    print("[OCR] Loading EasyOCR...")
    reader = easyocr.Reader(['en'], gpu=False)
except Exception as e:
    print(f"[OCR ERROR] {e}")

# ======================================
# 3. DATABASE HELPER
# ======================================
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Khởi tạo database với cấu trúc mới nhất (có RFID)"""
    with get_db_connection() as conn:
        c = conn.cursor()
        
        # Bảng lịch sử ra vào
        c.execute("""
            CREATE TABLE IF NOT EXISTS parking_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate TEXT,
                rfid_uid TEXT,
                entry_time TEXT,
                exit_time TEXT,
                fee REAL,
                image_path TEXT,
                status TEXT
            )
        """)
        
        # Bảng xe đăng ký vé tháng
        c.execute("""
            CREATE TABLE IF NOT EXISTS registered_vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plate TEXT UNIQUE,
                vehicle_type TEXT,
                owner TEXT,
                expiry_date TEXT
            )
        """)
        conn.commit()
    print("[DB] Database initialized.")

# Gọi khởi tạo ngay khi chạy
init_db()

# ======================================
# 4. LOGIC FUNCTIONS
# ======================================

def normalize_plate(text):
    """Chuẩn hóa biển số xe: Viết hoa và bỏ ký tự đặc biệt"""
    if not text: return None
    
    # 1. Chuyển thành chữ hoa
    plate = text.upper()
    
    # 2. Chỉ giữ lại Chữ (A-Z) và Số (0-9), bỏ hết dấu chấm, gạch ngang, khoảng trắng
    plate = re.sub(r'[^A-Z0-9]', '', plate) 

    # 3. Kiểm tra độ dài cơ bản (Biển VN thường từ 7-9 ký tự sau khi bỏ dấu)
    if len(plate) < 6 or len(plate) > 12:
        return None
        
    return plate

def calculate_fee(entry_str, exit_str=None):
    """Tính phí gửi xe"""
    if not entry_str: return 0, None
    
    fmt = "%Y-%m-%d %H:%M:%S"
    t1 = datetime.strptime(entry_str, fmt)
    
    if exit_str:
        t2 = datetime.strptime(exit_str, fmt)
    else:
        t2 = datetime.now()
        exit_str = t2.strftime(fmt)

    duration = (t2 - t1).total_seconds() / 60.0 # Phút
    
    # Logic: Miễn phí 15p đầu, sau đó 100đ/phút
    if duration <= 15:
        return 0, exit_str
    
    money = round(duration * 100) 
    return money, exit_str

def is_plate_registered(plate):
    """Kiểm tra biển số có trong danh sách đăng ký không"""
    if not plate: return False
    with get_db_connection() as conn:
        row = conn.execute("SELECT * FROM registered_vehicles WHERE plate = ?", (plate,)).fetchone()
        if row: return True # Có thể thêm logic check expiry_date tại đây
    return False

def capture_and_save(cam_ip, label):
    """Chụp ảnh từ Camera IP và lưu file"""
    url = f"http://{cam_ip}/capture"
    filename = f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    filepath = os.path.join(CAPTURE_FOLDER, filename)

    print(f"[CAM] Requesting {url}...")
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=4)
            if resp.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                return filepath
        except:
            time.sleep(0.5)
    return None

def process_plate_ai(image_path):
    """Xử lý AI: YOLO Detect -> Crop -> EasyOCR"""
    if not model or not reader:
        return None, "AI_NOT_READY"
    
    frame = cv2.imread(image_path)
    if frame is None: return None, "READ_ERR"

    results = model.predict(frame, conf=0.4, verbose=False)
    boxes = results[0].boxes
    if len(boxes) == 0: return None, "NO_DETECTION"

    best_box = max(boxes, key=lambda b: float(b.conf[0]))
    x1, y1, x2, y2 = map(int, best_box.xyxy[0].tolist())
    
    plate_img = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
    
    ocr_res = reader.readtext(gray)
    if not ocr_res: return None, "OCR_FAIL"
    
    raw_text = max(ocr_res, key=lambda r: len(r[1]))[1]
    return normalize_plate(raw_text), "SUCCESS"

def emit_realtime(event, payload):
    socketio.emit(event, payload)

# ======================================
# 5. WEB ROUTES (UI)
# ======================================

@app.route('/')
def dashboard():
    return render_template('dashboard.html')

@app.route('/history')
def history_page():
    return render_template('history.html')

@app.route('/registered')
def registered_page():
    return render_template('registered.html')

@app.route('/captures/<path:filename>')
def serve_capture(filename):
    return send_file(os.path.join(CAPTURE_FOLDER, filename))

# ======================================
# 6. API ROUTES (QUẢN LÝ DỮ LIỆU)
# ======================================
# ======================================
# API ROUTES (QUẢN LÝ DỮ LIỆU)
# ======================================
@app.route('/api/history', methods=['GET'])
def api_history():
    # Lấy tham số từ URL: ?start=2023-11-01&end=2023-11-05
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    
    conn = get_db_connection()
    
    # Nếu có chọn ngày, lọc theo ngày
    if start_date and end_date:
        # Thêm giờ vào để lấy trọn vẹn ngày bắt đầu và ngày kết thúc
        # VD: 2023-11-01 00:00:00 đến 2023-11-01 23:59:59
        start_str = f"{start_date} 00:00:00"
        end_str = f"{end_date} 23:59:59"
        
        sql = """
            SELECT * FROM parking_log 
            WHERE entry_time >= ? AND entry_time <= ? 
            ORDER BY id DESC
        """
        rows = conn.execute(sql, (start_str, end_str)).fetchall()
        
    else:
        # Mặc định: Lấy 50 tin mới nhất (như cũ)
        rows = conn.execute("SELECT * FROM parking_log ORDER BY id DESC LIMIT 50").fetchall()
        
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/registered', methods=['GET'])
def api_get_registered():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM registered_vehicles ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/registered', methods=['POST'])
def api_add_registered():
    data = request.json
    plate = normalize_plate(data.get('plate'))
    if not plate: return jsonify({"status": "error", "msg": "Invalid Plate"}), 400
    
    with get_db_connection() as conn:
        try:
            conn.execute("INSERT INTO registered_vehicles (plate, owner, vehicle_type) VALUES (?, ?, ?)",
                         (plate, data.get('owner'), data.get('type')))
            conn.commit()
        except:
            return jsonify({"status": "error", "msg": "Plate exists"}), 400
    return jsonify({"status": "ok"})

@app.route('/api/registered/<plate>', methods=['DELETE'])
def api_del_registered(plate):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM registered_vehicles WHERE plate=?", (plate,))
        conn.commit()
    return jsonify({"status": "ok"})

# ======================================
# 7. API ROUTES (HARDWARE LOGIC)
# ======================================

# --- API CẬP NHẬT CẢM BIẾN (ESP32 gửi lên) ---
@app.route('/api/update_data', methods=['POST'])
def api_update_data():
    data = request.json or {}
    slots = [int(data.get(f"s{i}", 0)) for i in range(1, 5)]
    
    payload = {
        "slots": slots,
        "free_slots": slots.count(0),
        "mq135": data.get("mq135", 0)
    }
    emit_realtime("sensor_update", payload)
    return jsonify({"status": "ok"})
# ======================================
# THÊM BIẾN TOÀN CỤC ĐỂ LƯU HÀNG ĐỢI LỆNH
# ======================================
command_queue = []

# ======================================
# API: NHẬN LỆNH TỪ WEB (ADMIN BẤM NÚT)
# ======================================
@app.route('/api/control/<action>', methods=['POST'])
def api_manual_control(action):
    global command_queue
    
    if action == "open_entry":
        command_queue.append("OPEN_ENTRY") # Bỏ lệnh vào hộp thư
        print("[MANUAL] Lệnh đã vào hàng đợi: OPEN_ENTRY")
        return jsonify({"status": "ok", "msg": "Đang mở cổng VÀO..."})
        
    elif action == "open_exit":
        command_queue.append("OPEN_EXIT") # Bỏ lệnh vào hộp thư
        print("[MANUAL] Lệnh đã vào hàng đợi: OPEN_EXIT")
        return jsonify({"status": "ok", "msg": "Đang mở cổng RA..."})
        
    return jsonify({"status": "error", "msg": "Lệnh không hợp lệ"}), 400

# ======================================
# API MỚI: ĐỂ ESP32 KIỂM TRA LỆNH (POLLING)
# ======================================
@app.route('/api/get_command', methods=['GET'])
def api_get_command():
    global command_queue
    if len(command_queue) > 0:
        # Lấy lệnh đầu tiên ra và gửi cho ESP32
        cmd = command_queue.pop(0)
        return jsonify({"command": cmd})
    else:
        return jsonify({"command": "none"})
# --- API CAMERA (Nhận diện biển số) ---
@app.route('/api/parking/<action>', methods=['POST'])
def api_parking_camera(action):
    """
    Xử lý xe vào/ra bằng Camera
    Action: 'entry' | 'exit'
    """
    cam_ip = CAM_ENTRY_IP if action == 'entry' else CAM_EXIT_IP
    
    # 1. Chụp ảnh
    img_path = capture_and_save(cam_ip, action)
    if not img_path:
        return jsonify({"status": "error", "msg": "Cam Fail", "action": f"deny_{action}"}), 500

    # 2. Nhận diện
    plate, note = process_plate_ai(img_path)
    if not plate: plate = "UNKNOWN"
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    web_img = f"/captures/{os.path.basename(img_path)}"

    # 3. Logic Entry
    if action == "entry":
        is_reg = is_plate_registered(plate)
        status = "Allowed" if is_reg else "Denied (Unregistered)"
        
        # Lưu Log
        with get_db_connection() as conn:
            conn.execute("INSERT INTO parking_log (plate, entry_time, image_path, status) VALUES (?, ?, ?, ?)",
                         (plate, now_str, img_path, "IN" if is_reg else "DENIED"))
            conn.commit()

        # Update UI
        emit_realtime("new_log", {"plate": plate, "action": "ENTRY (Cam)", "status": status, "time": now_str, "image": web_img})

        if is_reg:
            return jsonify({"status": "ok", "action": "allow_entry", "plate": plate})
        else:
            return jsonify({"status": "denied", "action": "deny_entry", "plate": plate}), 403

    # 4. Logic Exit
    elif action == "exit":
        with get_db_connection() as conn:
            # Tìm lượt vào gần nhất của biển này
            row = conn.execute("SELECT * FROM parking_log WHERE plate=? AND status='IN' ORDER BY id DESC LIMIT 1", (plate,)).fetchone()
            
            fee = 0
            if row:
                fee, exit_time = calculate_fee(row['entry_time'])
                conn.execute("UPDATE parking_log SET exit_time=?, fee=?, status='OUT' WHERE id=?", (exit_time, fee, row['id']))
                conn.commit()
                status = "Out"
            else:
                status = "Not Found"

        emit_realtime("new_log", {"plate": plate, "action": f"EXIT (Fee: {fee})", "status": status, "time": now_str, "image": web_img})

        if status == "Out":
            if fee > 0:
                return jsonify({"status": "ok", "action": "payment_due", "fee": fee, "plate": plate})
            else:
                return jsonify({"status": "ok", "action": "allow_exit", "plate": plate})
        else:
             return jsonify({"status": "error", "action": "deny_exit", "msg": "No Entry Record"}), 404

    return jsonify({"status": "err"}), 400

# --- API RFID (Vé lượt / Thẻ từ) ---
@app.route('/api/rfid/<action>', methods=['POST'])
def api_rfid_handler(action):
    """
    Xử lý thẻ từ (Vé lượt cho khách vãng lai)
    Action: 'entry' | 'exit'
    Payload: {"uid": "XX XX XX XX"}
    """
    data = request.json or {}
    uid = data.get("uid", "").strip()
    if not uid: return jsonify({"status": "error"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    web_img = "/static/rfid_icon.png" # Icon mặc định cho thẻ từ

    # Logic Entry (RFID)
    if action == "entry":
        with get_db_connection() as conn:
            # Kiểm tra xem thẻ này có đang gửi xe không
            exist = conn.execute("SELECT * FROM parking_log WHERE rfid_uid=? AND status='IN'", (uid,)).fetchone()
            if exist:
                 return jsonify({"status": "error", "msg": "Card busy", "action": "deny_entry"}), 400
            
            # Tạo vé lượt
            conn.execute("INSERT INTO parking_log (plate, rfid_uid, entry_time, image_path, status) VALUES (?, ?, ?, ?, ?)",
                         ("GUEST_RFID", uid, now_str, web_img, "IN"))
            conn.commit()
        
        emit_realtime("new_log", {"plate": f"RFID: {uid}", "action": "ENTRY (Guest)", "status": "Allowed", "time": now_str, "image": web_img})
        return jsonify({"status": "ok", "action": "allow_entry", "uid": uid})

    # Logic Exit (RFID)
    elif action == "exit":
        with get_db_connection() as conn:
            row = conn.execute("SELECT * FROM parking_log WHERE rfid_uid=? AND status='IN' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
            
            if not row:
                return jsonify({"status": "error", "action": "deny_exit"}), 404
            
            fee, exit_time = calculate_fee(row['entry_time'])
            conn.execute("UPDATE parking_log SET exit_time=?, fee=?, status='OUT' WHERE id=?", (exit_time, fee, row['id']))
            conn.commit()

        emit_realtime("new_log", {"plate": f"RFID: {uid}", "action": "EXIT (Guest)", "status": "Out", "fee": fee, "time": exit_time, "image": web_img})
        
        # Vé lượt luôn tính tiền (trừ khi < 15p)
        action_resp = "payment_due" if fee > 0 else "allow_exit"
        return jsonify({"status": "ok", "action": action_resp, "fee": fee, "uid": uid})

    return jsonify({"status": "err"}), 400


# ======================================
# 8. RUN SERVER
# ======================================
if __name__ == '__main__':
    print(f"Server starting on {AI_SERVER_IP}:{PORT}")
    socketio.run(app, host=AI_SERVER_IP, port=PORT, debug=True)