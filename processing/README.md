# MPU6050 3D visualizer

El firmware del ESP32 envía muestras del MPU6050 como registros seriales
delimitados por salto de línea:

```text
DATA,ax,ay,az,gx,gy,gz,temperature
```

La aceleración va en m/s², el giro en rad/s y la temperatura en °C. El sketch
de Processing convierte el giro a grados por segundo y combina la integración
del giroscopio con la inclinación del acelerómetro (filtro complementario,
α = 0.96) para obtener roll y pitch. El MPU6050 se muestrea a 50 Hz; los
diagnósticos de los demás sensores se envían cada dos segundos. El yaw se
obtiene solo del giroscopio y deriva con el tiempo.

## Convención de ejes

El dibujo mapea los ejes del sensor así:

| Eje del sensor | Color | Pantalla |
|---|---|---|
| +X | rojo | derecha |
| +Y | verde | arriba |
| +Z | azul | hacia el espectador |

Ese mapeo refleja el sistema de ejes, por lo que **roll y yaw se aplican con
signo negativo** al rotar la escena (`INVERT_ROLL` / `INVERT_YAW` en el sketch).
Si al mover la placa la tetera se mueve espejada respecto del movimiento real,
invierte esos dos flags.

## Cómo se ejecuta

1. Sube el firmware de PlatformIO al ESP32.
2. Cierra el Monitor Serial de PlatformIO: solo un programa puede usar el
   puerto serie a la vez.
3. Abre `MPU6050_Visualizer/MPU6050_Visualizer.pde` en Processing 4 y ejecútalo.
4. El sketch selecciona el puerto USB automáticamente. Si no puede identificarlo
   o hay varios equivalentes, mira la lista de puertos en la consola de
   Processing y asigna el índice a `SERIAL_PORT_INDEX` al inicio del sketch.
5. Mantén la velocidad en 115200. El sketch muestra el estado "conectado"
   cuando recibe registros `DATA` válidos.

## Arranque y calibración

Los primeros segundos el sketch muestra **CALIBRANDO GIROSCOPIO**: mantén la
placa en reposo hasta que desaparezca el aviso (~1 s, 50 muestras). Eso mide el
bias del giroscopio y reduce mucho la deriva del yaw.

Si el sketch dice que está esperando datos, comprueba que el puerto seleccionado
es el del ESP32, que el firmware se subió bien y que no hay ningún monitor
serie abierto.

## Notas de robustez

- Si el flujo de datos se corta más de 500 ms, el filtro se reinicia solo para
  que un hueco no se integre como una rotación enorme.
- El sketch reintenta abrir el puerto cada 2 s si estaba ocupado (por ejemplo,
  si el Monitor Serial de PlatformIO lo tenía tomado).
- El firmware valida el rango físico de cada muestra antes de emitir `DATA`;
  un cable I2C suelto se reporta como `[MPU6050] I2C ERROR` en lugar de enviar
  valores basura que arruinarían la orientación.
- El HX711 está desactivado por defecto (`ENABLE_HX711 0` en `src/main.cpp`):
  la librería bogde/HX711 puede bloquear el bucle sin timeout si el sensor se
  desconecta en caliente. Actívalo solo cuando el HX711 esté conectado.
