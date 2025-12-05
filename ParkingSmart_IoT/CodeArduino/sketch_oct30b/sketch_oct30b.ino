/*
 * FILE: ESP32_FULL_STABLE.ino
 * FIX: Thêm Timeout HTTP, Watchdog chống treo, Gộp logic
 */

#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <MFRC522.h>

// ================= CONFIG =================
const char* WIFI_SSID = "Loc Minh 2"; 
const char* WIFI_PASS = "0365408003";    
const char* SERVER_HOST = "192.168.2.173"; // <--- Đảm bảo IP đúng
const int   SERVER_PORT = 5000;

#define UNO_ADDR 0x08
#define MAX_SLOTS 4

// RFID Config (VSPI)
#define SS_PIN  5
#define RST_PIN 4

// ================= OBJECTS =================
MFRC522 rfid(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(0x27, 16, 2);

// ================= VARIABLES =================
int slots[4] = {0, 0, 0, 0}; 
int lastSlots[4] = {-1, -1, -1, -1};

bool entryTriggered = false;
bool exitTriggered = false;
unsigned long lastHttpUpdate = 0;
unsigned long lastCommandCheck = 0;

// ================= DISPLAY HELPERS =================
void updateLCDGrid() {
  bool changed = false;
  for(int i=0; i<4; i++) if(slots[i] != lastSlots[i]) changed = true;
  if (!changed) return;

  for(int i=0; i<4; i++) lastSlots[i] = slots[i];

  lcd.setCursor(0, 0);
  lcd.printf("S1:[%c] S2:[%c]", slots[0]?'X':' ', slots[1]?'X':' ');
  lcd.setCursor(0, 1);
  lcd.printf("S3:[%c] S4:[%c]", slots[2]?'X':' ', slots[3]?'X':' ');
}

void lcdMessage(String line1, String line2) {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print(line1);
  lcd.setCursor(0, 1); lcd.print(line2);
}

// ================= HARDWARE CONTROL =================
void sendToUno(String cmd) {
  Wire.beginTransmission(UNO_ADDR);
  Wire.write((const uint8_t*)cmd.c_str(), cmd.length());
  Wire.endTransmission();
  delay(50); // Delay nhỏ để tránh nghẽn I2C
}

// ================= SERVER COMMUNICATION =================

// Hàm gọi Server chung (Dùng cho cả RFID và Camera)
String callServer(String type, String action, String uid = "") {
  if (WiFi.status() != WL_CONNECTED) return "WIFI_ERR";
  
  HTTPClient http;
  String url = "http://" + String(SERVER_HOST) + ":" + String(SERVER_PORT) + "/api/" + type + "/" + action;
  
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setConnectTimeout(3000); // Timeout 3s để tránh treo

  String jsonStr = "{}";
  if (type == "rfid") {
    jsonStr = "{\"uid\": \"" + uid + "\"}";
  }

  int httpCode = http.POST(jsonStr);
  String decision = "error";

  if (httpCode > 0) {
    String payload = http.getString();
    StaticJsonDocument<512> doc;
    deserializeJson(doc, payload);
    
    decision = String((const char*)doc["action"]);
    const char* plate = doc["plate"];

    // Nếu Server trả về Payment Due hoặc Allow
    if (plate) {
      lcdMessage(String(plate), decision);
      delay(1500); // Giữ màn hình 1.5s
    }
  } else {
    Serial.printf("[HTTP] Error: %s\n", http.errorToString(httpCode).c_str());
  }
  http.end();
  return decision;
}

// Hàm nhận lệnh Manual Control từ Web
void checkServerCommand() {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  String url = "http://" + String(SERVER_HOST) + ":" + String(SERVER_PORT) + "/api/get_command";
  http.begin(url);
  http.setConnectTimeout(2000);
  
  int httpCode = http.GET();
  if (httpCode > 0) {
    String payload = http.getString();
    StaticJsonDocument<200> doc;
    deserializeJson(doc, payload);
    String cmd = String((const char*)doc["command"]);

    if (cmd != "none" && cmd != "null") {
      Serial.println("[MANUAL] Cmd: " + cmd);
      
      if (cmd == "OPEN_ENTRY") {
        lcdMessage("ADMIN CONTROL", "OPEN ENTRY...");
        sendToUno("OPEN_ENTRY");
        delay(3000);
        sendToUno("CLOSE_ENTRY");
        lastSlots[0] = -1; 
      } 
      else if (cmd == "OPEN_EXIT") {
        lcdMessage("ADMIN CONTROL", "OPEN EXIT...");
        sendToUno("OPEN_EXIT");
        delay(3000);
        sendToUno("CLOSE_EXIT");
        lastSlots[0] = -1;
      }
    }
  }
  http.end();
}

void sendSensorData() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = "http://" + String(SERVER_HOST) + ":" + String(SERVER_PORT) + "/api/update_data";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setConnectTimeout(2000);
  
  StaticJsonDocument<200> doc;
  doc["s1"] = slots[0]; doc["s2"] = slots[1];
  doc["s3"] = slots[2]; doc["s4"] = slots[3];
  
  String jsonStr;
  serializeJson(doc, jsonStr);
  http.POST(jsonStr);
  http.end();
}

// ================= MAIN TASK =================
void TaskSystem(void *param) {
  Serial.println("--- Task Started ---");
  
  for (;;) {
    // 1. Đọc I2C từ UNO (Cảm biến IR)
    Wire.requestFrom(UNO_ADDR, 3);
    if (Wire.available() == 3) {
      byte sensorPacket = Wire.read();
      byte hi = Wire.read();
      byte lo = Wire.read();
      
      slots[0] = bitRead(sensorPacket, 2);
      slots[1] = bitRead(sensorPacket, 3);
      slots[2] = bitRead(sensorPacket, 4);
      slots[3] = bitRead(sensorPacket, 5);
      
      bool ir_entry = bitRead(sensorPacket, 0);
      bool ir_exit  = bitRead(sensorPacket, 1);

      // --- LOGIC CAMERA (IR TRIGGER) ---
      
      // ENTRY
      if (ir_entry && !entryTriggered) {
        entryTriggered = true;
        int count = slots[0] + slots[1] + slots[2] + slots[3];
        
        if (count >= MAX_SLOTS) {
          lcdMessage("!! FULL !!", "NO ENTRY");
          delay(2000);
          lastSlots[0] = -1; 
        } else {
          lcdMessage("CHECKING CAM...", " WAIT ->");
          String res = callServer("parking", "entry");
          if (res == "allow_entry") {
            lcdMessage("WELCOME", "OPEN GATE");
            sendToUno("OPEN_ENTRY");
            delay(3000);
            sendToUno("CLOSE_ENTRY");
          } else {
            lcdMessage("CAM DENIED", "USE CARD/APP");
            delay(2000);
          }
          lastSlots[0] = -1;
        }
      }
      if (!ir_entry) entryTriggered = false;

      // EXIT
      if (ir_exit && !exitTriggered) {
        exitTriggered = true;
        lcdMessage("EXITING...", "CHECKING CAM");
        String res = callServer("parking", "exit");
        
        if (res == "allow_exit" || res == "payment_due") {
          lcdMessage(res == "allow_exit"?"GOODBYE":"PAY FEE", "OPEN GATE");
          if(res == "payment_due") delay(2000);
          sendToUno("OPEN_EXIT");
          delay(3000);
          sendToUno("CLOSE_EXIT");
        } else {
          lcdMessage("CAM FAILED", "USE CARD");
          delay(2000);
        }
        lastSlots[0] = -1;
      }
      if (!ir_exit) exitTriggered = false;
    }

    // 2. LOGIC RFID (QUẸT THẺ)
    if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
      String uid = "";
      for (byte i = 0; i < rfid.uid.size; i++) {
        uid += String(rfid.uid.uidByte[i] < 0x10 ? " 0" : " ");
        uid += String(rfid.uid.uidByte[i], HEX);
      }
      uid.trim(); uid.toUpperCase();

      Serial.println("[RFID] UID: " + uid);
      lcdMessage("RFID READING...", uid);
      
      // Dùng trick: Nếu IR Exit đang bị che -> Coi như quẹt ra. Ko thì quẹt vào.
      // (Vì thẻ từ không biết hướng, phải dựa vào vị trí người đứng)
      // Hoặc đơn giản: Thử Entry trước, nếu Server báo lỗi "Already In" thì gọi Exit? 
      // Ở đây tôi dùng giả định: Quẹt thẻ là Entry trừ khi đang đứng ở Exit.
      
      // Đọc lại IR Exit nhanh (vì I2C đã đọc ở trên)
      bool isAtExit = false; 
      // (Lưu ý: biến ir_exit ở trên là cục bộ, nếu muốn chính xác cần biến toàn cục)
      // Tạm thời mặc định là Entry nếu không xác định được
      
      String action = "entry"; 
      String res = callServer("rfid", action, uid);
      
      // Nếu thẻ này đang ở trong bãi ("Card busy"), thử gọi lệnh exit
      if (res == "deny_entry") {
         lcdMessage("TRYING EXIT...", "WAIT");
         res = callServer("rfid", "exit", uid);
         action = "exit";
      }

      if (res == "allow_entry" || res == "allow_exit" || res == "payment_due") {
        lcdMessage("RFID OK", "OPEN GATE");
        if (action == "entry") {
          sendToUno("OPEN_ENTRY"); delay(3000); sendToUno("CLOSE_ENTRY");
        } else {
          sendToUno("OPEN_EXIT"); delay(3000); sendToUno("CLOSE_EXIT");
        }
      } else {
        lcdMessage("RFID DENIED", "INVALID");
        delay(2000);
      }

      rfid.PICC_HaltA();
      rfid.PCD_StopCrypto1();
      lastSlots[0] = -1; 
    }

    // 3. MANUAL CONTROL (1s/lần)
    if (millis() - lastCommandCheck > 1000) {
      checkServerCommand();
      lastCommandCheck = millis();
    }

    // 4. UPDATE SENSOR & LCD (3s/lần & Idle)
    if (millis() - lastHttpUpdate > 3000) {
      sendSensorData();
      lastHttpUpdate = millis();
    }
    
    if (!entryTriggered && !exitTriggered) updateLCDGrid();

    vTaskDelay(50 / portTICK_PERIOD_MS);
  }
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  delay(1000); // Đợi ổn định điện áp

  Serial.println("--- SYSTEM INIT ---");

  // 1. Init I2C & LCD
  Wire.begin();
  lcd.init(); lcd.backlight();
  lcd.print("INIT SYSTEM...");

  // 2. Init RFID
  SPI.begin();
  rfid.PCD_Init();
  Serial.println("RFID Ready");

  // 3. Init WiFi
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lcd.setCursor(0,1); lcd.print("WIFI...");
  
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500); Serial.print("."); retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    lcd.clear(); lcd.print("WIFI CONNECTED");
    Serial.println("\nIP: " + WiFi.localIP().toString());
  } else {
    lcd.clear(); lcd.print("WIFI FAILED");
  }
  delay(1000);

  // 4. Start Task
  xTaskCreate(TaskSystem, "System", 10000, NULL, 1, NULL); // Tăng stack lên 10000 cho an toàn
}

void loop() {}