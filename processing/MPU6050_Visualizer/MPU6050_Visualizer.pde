import processing.serial.*;

// ============================================================
// ESP32 + MPU6050 | Visualizacion 3D en tiempo real
// Tesis: IoT-WSN y CCTV - Subsistema Estructural
//
// Protocolo: el firmware envia "DATA,ax,ay,az,gx,gy,gz,temp" a 50 Hz
//   aceleracion en m/s^2, giro en rad/s, temperatura en C.
// ============================================================

final int SERIAL_PORT_INDEX = -1;   // -1 = autodetectar
final int SERIAL_BAUD = 115200;

// Convencion de ejes del dibujo:
//   sensor +X -> pantalla derecha (rojo)
//   sensor +Y -> pantalla arriba   (verde)
//   sensor +Z -> pantalla afuera   (azul)
// Ese mapeo es un REFLEJO (invierte el sentido de las rotaciones en X y Z),
// por eso roll y yaw se rotan con signo negativo en drawScene().
// Si al mover la placa la tetera se mueve espejada, invierte estos dos flags.
final boolean INVERT_ROLL = true;
final boolean INVERT_YAW  = true;

// Calibracion del bias del giroscopio (mantener la placa en reposo).
final int GYRO_BIAS_SAMPLES = 50;   // ~1 s a 50 Hz

Serial esp32;
String selectedPort = "";
boolean sensorConnected = false;
int lastDataMillis = 0;
int previousSampleMillis = 0;
boolean hasFirstSample = false;

// Estado del filtro. Lo escribe serialEvent (hilo del puerto serie) y lo lee
// draw (hilo de animacion); todo acceso va bajo dataLock.
final Object dataLock = new Object();
float rollDegrees = 0;
float pitchDegrees = 0;
float yawDegrees = 0;
float accelX = 0;
float accelY = 0;
float accelZ = 0;
float gyroX = 0;
float gyroY = 0;
float gyroZ = 0;
float temperatureC = 0;

// Bias del giroscopio (rad/s), medido con la placa en reposo.
float gyroBiasX = 0;
float gyroBiasY = 0;
float gyroBiasZ = 0;
boolean gyroCalibrated = false;
int gyroBiasCount = 0;
float gyroBiasSumX = 0, gyroBiasSumY = 0, gyroBiasSumZ = 0;

// Instantanea coherente para dibujar (se copia al inicio de cada frame).
float fRoll, fPitch, fYaw, fAx, fAy, fAz, fGx, fGy, fGz, fTemp;
boolean fConnected;
int fLastDataMillis;

void setup() {
  size(1200, 760, P3D);
  surface.setTitle("ESP32 + MPU6050 | Visualizacion 3D");
  smooth(8);
  frameRate(60);

  tryConnect();
}

void tryConnect() {
  String[] ports = Serial.list();
  println("Puertos seriales disponibles:");
  for (int i = 0; i < ports.length; i++) {
    println("[" + i + "] " + ports[i]);
  }

  if (ports.length == 0) {
    println("No se encontraron puertos seriales. Conecta la ESP32.");
    selectedPort = "(sin puerto)";
    return;
  }

  int portIndex = findEsp32Port(ports);
  if (portIndex < 0) {
    println("No pude identificar el puerto de la ESP32.");
    println("Cambia SERIAL_PORT_INDEX al indice correcto y vuelve a ejecutar.");
    selectedPort = "(no identificado)";
    return;
  }

  selectedPort = ports[portIndex];
  println("Conectando a " + selectedPort + " a " + SERIAL_BAUD + " baudios.");
  println("Cierra el Monitor Serial de PlatformIO antes de ejecutar Processing.");

  try {
    esp32 = new Serial(this, selectedPort, SERIAL_BAUD);
    esp32.clear();
    esp32.bufferUntil('\n');
    resetFilter();
    println("Puerto abierto correctamente.");
  } catch (Exception e) {
    // jSSC lanza RuntimeException si el puerto esta ocupado o desaparece.
    println("No pude abrir " + selectedPort + ": " + e.getMessage());
    println("Cierra el Monitor Serial de PlatformIO y vuelve a intentarlo.");
    esp32 = null;
    selectedPort = "(ocupado: " + selectedPort + ")";
  }
}

void resetFilter() {
  synchronized (dataLock) {
    hasFirstSample = false;
    gyroCalibrated = false;
    gyroBiasCount = 0;
    gyroBiasSumX = gyroBiasSumY = gyroBiasSumZ = 0;
    rollDegrees = pitchDegrees = yawDegrees = 0;
  }
}

int findEsp32Port(String[] ports) {
  if (SERIAL_PORT_INDEX >= 0) {
    return SERIAL_PORT_INDEX < ports.length ? SERIAL_PORT_INDEX : -1;
  }

  int match = -1;
  int bestScore = 0;
  for (int i = 0; i < ports.length; i++) {
    String name = ports[i].toLowerCase();
    boolean usbSerial = name.contains("usbserial") || name.contains("usbmodem")
      || name.contains("slab_usbtouart") || name.contains("wchusbserial");
    if (!usbSerial) {
      continue;
    }

    int score = name.startsWith("/dev/cu.") ? 2 : 1;
    if (score > bestScore) {
      match = i;
      bestScore = score;
    } else if (score == bestScore) {
      println("Hay mas de un puerto USB serial equivalente; selecciona el indice manualmente.");
      return -1;
    }
  }

  if (match >= 0) {
    return match;
  }
  return ports.length == 1 ? 0 : -1;
}

void draw() {
  // Copia coherente del estado para este frame.
  synchronized (dataLock) {
    fRoll = rollDegrees;
    fPitch = pitchDegrees;
    fYaw = yawDegrees;
    fAx = accelX;
    fAy = accelY;
    fAz = accelZ;
    fGx = gyroX;
    fGy = gyroY;
    fGz = gyroZ;
    fTemp = temperatureC;
    fConnected = sensorConnected;
    fLastDataMillis = lastDataMillis;
  }

  // Si el flujo de datos se corta, se re-inicia el filtro al volver: evita que
  // un hueco largo se integre como una rotacion gigante.
  if (fConnected && millis() - fLastDataMillis > 500) {
    synchronized (dataLock) {
      sensorConnected = false;
    }
    resetFilter();
  }

  // Reconexion: si no hay puerto o quedo ocupado, reintentar cada 2 s.
  if (esp32 == null && frameCount % 120 == 0) {
    tryConnect();
  }

  background(12, 18, 31);
  drawScene();

  hint(DISABLE_DEPTH_TEST);
  camera();
  drawDashboard();
  hint(ENABLE_DEPTH_TEST);
}

void serialEvent(Serial port) {
  String line = port.readStringUntil('\n');
  if (line == null) {
    return;
  }
  line = trim(line);
  if (!line.startsWith("DATA,")) {
    return;
  }

  String[] fields = split(line, ',');
  if (fields.length != 8) {
    return;
  }

  float[] sample = new float[7];
  for (int i = 0; i < sample.length; i++) {
    sample[i] = parseFloat(fields[i + 1]);
    if (Float.isNaN(sample[i]) || Float.isInfinite(sample[i])) {
      return;
    }
  }

  synchronized (dataLock) {
    accelX = sample[0];
    accelY = sample[1];
    accelZ = sample[2];
    gyroX = sample[3];
    gyroY = sample[4];
    gyroZ = sample[5];
    temperatureC = sample[6];
    updateOrientation();
    sensorConnected = true;
    lastDataMillis = millis();
  }
}

void updateOrientation() {
  float accelRoll = degrees(atan2(accelY, accelZ));
  // atan2 con denominador >= 0 confina el pitch a [-90, 90].
  float accelPitch = degrees(atan2(-accelX, sqrt(accelY * accelY + accelZ * accelZ)));
  int now = millis();

  // --- Calibracion del bias del giroscopio con la placa en reposo ---
  if (!gyroCalibrated) {
    gyroBiasSumX += gyroX;
    gyroBiasSumY += gyroY;
    gyroBiasSumZ += gyroZ;
    gyroBiasCount++;
    if (gyroBiasCount >= GYRO_BIAS_SAMPLES) {
      gyroBiasX = gyroBiasSumX / GYRO_BIAS_SAMPLES;
      gyroBiasY = gyroBiasSumY / GYRO_BIAS_SAMPLES;
      gyroBiasZ = gyroBiasSumZ / GYRO_BIAS_SAMPLES;
      gyroCalibrated = true;
      println("Bias del giroscopio calibrado: "
        + nf(gyroBiasX, 0, 4) + ", " + nf(gyroBiasY, 0, 4) + ", " + nf(gyroBiasZ, 0, 4) + " rad/s");
    }
    previousSampleMillis = now;
    return;
  }

  float bx = gyroX - gyroBiasX;
  float by = gyroY - gyroBiasY;
  float bz = gyroZ - gyroBiasZ;

  if (!hasFirstSample) {
    rollDegrees = accelRoll;
    pitchDegrees = accelPitch;
    previousSampleMillis = now;
    hasFirstSample = true;
    return;
  }

  // dt del reloj de llegada, acotado: si el driver entrega varias lineas juntas
  // (dt~0) o se produce un hueco, la integracion no se deforma.
  float dt = (now - previousSampleMillis) / 1000.0;
  dt = constrain(dt, 0.005, 0.06);
  previousSampleMillis = now;
  float gyroToDegrees = 180.0 / PI;
  float alpha = 0.96;

  // Prediccion con el giroscopio.
  rollDegrees += bx * gyroToDegrees * dt;
  pitchDegrees += by * gyroToDegrees * dt;
  yawDegrees += bz * gyroToDegrees * dt;

  // Correccion con el acelerometro usando el ERROR angular desenvuelto: sin
  // esto, la mezcla salta al cruzar +-180 de roll o +-90 de pitch.
  float errPitch = wrap180(accelPitch - pitchDegrees);
  pitchDegrees += (1.0 - alpha) * errPitch;

  // Cerca de pitch +-90 el roll medido por el acelerometro se vuelve singular
  // (atan2 sobre un vector casi vertical), asi que solo se corrige el roll
  // fuera de esa zona.
  if (abs(pitchDegrees) < 80) {
    float errRoll = wrap180(accelRoll - rollDegrees);
    rollDegrees += (1.0 - alpha) * errRoll;
  }

  rollDegrees = wrap180(rollDegrees);
  pitchDegrees = constrain(pitchDegrees, -90, 90);
  yawDegrees = wrap180(yawDegrees);
}

// Envuelve un angulo al rango (-180, 180].
float wrap180(float a) {
  a = a % 360.0;
  if (a > 180.0) a -= 360.0;
  if (a <= -180.0) a += 360.0;
  return a;
}

void drawScene() {
  pushMatrix();
  translate(width * 0.35, height * 0.55, 0);
  // El mapeo sensor->pantalla refleja el sistema de ejes, por eso roll y yaw
  // entran negados (ver INVERT_ROLL / INVERT_YAW arriba).
  float yawDeg = INVERT_YAW ? -fYaw : fYaw;
  float rollDeg = INVERT_ROLL ? -fRoll : fRoll;
  rotateZ(radians(yawDeg));
  rotateY(radians(fPitch));
  rotateX(radians(rollDeg));

  drawAxes();
  drawTeapot();
  popMatrix();
}

void drawAxes() {
  strokeWeight(3);
  stroke(255, 90, 90);
  line(0, 0, 0, 150, 0, 0);
  stroke(100, 235, 150);
  line(0, 0, 0, 0, -150, 0);
  stroke(100, 170, 255);
  line(0, 0, 0, 0, 0, 150);
}

void drawTeapot() {
  noStroke();

  pushMatrix();
  fill(42, 183, 190);
  scale(1.0, 0.82, 0.82);
  sphereDetail(36);
  sphere(82);
  popMatrix();

  pushMatrix();
  translate(0, -67, 0);
  fill(55, 205, 205);
  sphere(34);
  translate(0, -35, 0);
  fill(245, 177, 75);
  sphere(13);
  popMatrix();

  drawSpout();

  pushMatrix();
  noFill();
  stroke(245, 177, 75);
  strokeWeight(18);
  bezier(-66, -28, 0, -150, -45, 0, -150, 45, 0, -66, 55, 0);
  popMatrix();

  pushMatrix();
  translate(-66, -28, 0);
  noStroke();
  fill(245, 177, 75);
  sphere(10);
  translate(0, 83, 0);
  sphere(10);
  popMatrix();
}

void drawSpout() {
  pushMatrix();
  noStroke();
  fill(42, 183, 190);
  beginShape(TRIANGLE_STRIP);
  int segments = 32;
  for (int i = 0; i <= segments; i++) {
    float angle = TWO_PI * i / segments;
    float y = cos(angle);
    float z = sin(angle);
    vertex(53, y * 34, z * 34);
    vertex(145, y * 12, z * 12);
  }
  endShape();
  popMatrix();
}

void drawDashboard() {
  boolean live = fConnected && millis() - fLastDataMillis < 3000;

  fill(239, 245, 255);
  textSize(25);
  text("MPU6050  |  VISUALIZACION 3D", 42, 48);

  fill(156, 174, 198);
  textSize(14);
  text("ESP32 · orientacion en tiempo real", 44, 75);

  fill(live ? color(80, 220, 145) : color(255, 184, 80));
  textSize(16);
  String status = live
    ? "●  MPU6050 conectado · " + selectedPort
    : "●  Esperando datos DATA,... · " + selectedPort;
  text(status, 44, 112);

  if (!gyroCalibrated) {
    fill(255, 184, 80);
    textSize(15);
    text("CALIBRANDO GIROSCOPIO — mantenga la placa en reposo ("
      + gyroBiasCount + "/" + GYRO_BIAS_SAMPLES + ")", 44, 140);
  }

  fill(239, 245, 255);
  textSize(19);
  text("ORIENTACION", 730, 190);
  drawValue("Roll", fRoll, "°", 730, 235);
  drawValue("Pitch", fPitch, "°", 730, 285);
  drawValue("Yaw*", fYaw, "°", 730, 335);

  fill(239, 245, 255);
  textSize(19);
  text("ACELEROMETRO", 730, 410);
  drawValue("X", fAx, "m/s²", 730, 455);
  drawValue("Y", fAy, "m/s²", 730, 495);
  drawValue("Z", fAz, "m/s²", 730, 535);

  fill(239, 245, 255);
  textSize(19);
  text("GIROSCOPIO", 970, 410);
  drawValue("X", degrees(fGx - gyroBiasX), "°/s", 970, 455);
  drawValue("Y", degrees(fGy - gyroBiasY), "°/s", 970, 495);
  drawValue("Z", degrees(fGz - gyroBiasZ), "°/s", 970, 535);

  fill(156, 174, 198);
  textSize(14);
  text("Temperatura MPU6050: " + nf(fTemp, 0, 1) + " °C", 730, 590);
  text("* El yaw se integra con el giroscopio; deriva menos tras calibrar el bias.", 730, 620);
  text("Mueve la placa suavemente para ver cambiar la orientacion.", 730, 650);
  text("Rojo: X    Verde: Y    Azul: Z", 44, height - 34);
}

void drawValue(String label, float value, String unit, float x, float y) {
  fill(156, 174, 198);
  textSize(15);
  text(label, x, y);
  fill(239, 245, 255);
  textSize(17);
  text(nf(value, 0, 2) + " " + unit, x + 82, y);
}
