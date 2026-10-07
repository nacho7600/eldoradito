# Puente Tuya → Windguru / Weathercloud

Lee una estación meteorológica **Tuya** (la que reporta solo a `iot.tuya.com`) y publica sus
datos en **Windguru** y/o **Weathercloud**, corriendo en los servidores de GitHub con un cron.
**No necesita ninguna PC encendida.**

```
 [estación Tuya] --WiFi--> iot.tuya.com  <--HTTPS--  [GitHub Actions, cada 10 min]
                                                              |
                                                  +-----------+-----------+
                                                  v                       v
                                        windguru.cz/upload      api.weathercloud.net
```

## Qué hace cada archivo

| Archivo | Función |
|---|---|
| `sync_tuya.py` | Lee el estado de la estación en Tuya, lo traduce y lo publica. Las credenciales las toma de variables de entorno. |
| `mapeo.json` | Traduce los códigos de Tuya (`temp_current_external`, `windspeed_avg`, ...) a los datos que entienden las redes. Se edita sin tocar el código. |
| `.github/workflows/puente.yml` | El cron de GitHub: cada 10 minutos ejecuta el script y guarda el historial en el repo. |
| `data/historial.csv` | Una fila por lectura. GitHub guarda cada commit, así que el repo es el archivo histórico. |

## Puesta en marcha (una sola vez, ~10 minutos)

1. **Crear el repositorio** en github.com → *New repository*.
   - Nombre sugerido: `estacion-tuya` (o `doradito-tuya`).
   - **Público** si querés cada 10 minutos gratis (los secretos quedan igual de ocultos:
     van en *Secrets*, nunca en el código). **Privado** también sirve, pero mirá el cupo
     más abajo (conviene cada hora).
2. **Subir estos archivos** (o hacer `git push`, si preferís la consola):
   `sync_tuya.py`, `mapeo.json`, la carpeta `.github/workflows/puente.yml` y `data/historial.csv`.
3. **Cargar los secretos**: repo → *Settings → Secrets and variables → Actions →
   New repository secret*:

   | Secret | Valor |
   |---|---|
   | `TUYA_ACCESS_ID` | Access ID / Client ID del proyecto en iot.tuya.com |
   | `TUYA_ACCESS_SECRET` | Access Secret / Client Secret |
   | `TUYA_DEVICE_ID` | Device ID de la estación (pestaña *Devices* del proyecto) |
   | `WINDGURU_UID` | uid de la estación en Windguru (tipo *Other (upload API)*) |
   | `WINDGURU_PASSWORD` | password de esa estación en Windguru |
   | `WEATHERCLOUD_WID` | *(opcional)* wid del dispositivo en Weathercloud |
   | `WEATHERCLOUD_KEY` | *(opcional)* key de ese dispositivo |

   Si no cargás los de Weathercloud, solo publica en Windguru (y al revés).
4. **Primera corrida de prueba**: pestaña *Actions* → workflow *Puente Tuya a Windguru* →
   *Run workflow* → marcar **Solo leer y mostrar (no publica)** → *Run*. En el log vas a ver
   los datos que leyó, sin que se publique nada.
5. **Corrida real**: *Run workflow* sin marcar la casilla. De ahí en más corre solo.

## Frecuencia, cupos y límites

- **Repo público**: GitHub Actions es gratis y sin límite de minutos → cada 10 minutos.
- Este repositorio es **público**, así que los minutos de GitHub Actions son **gratis e
  ilimitados** y el cron corre **cada 10 minutos** (`*/10 * * * *`). (En un repo privado el
  plan gratuito da 2.000 min/mes: cada 10 minutos serían ~4.300 y no entraría; ahí habría
  que usar cada hora, `0 * * * *`.)
- Los **secretos no se publican**: viven en *Settings → Secrets*, están cifrados y solo se
  inyectan al correr el workflow. En este repositorio no hay ninguna credencial, ni en el
  código ni en la historia de commits.
- El **cron de GitHub no es exacto**: puede demorarse varios minutos o saltear alguna corrida.
  Para Windguru no es problema (acepta datos de hasta 2 horas de antigüedad).
- **Weathercloud** rechaza envíos más seguidos que 10 minutos en el plan gratuito.
- GitHub **desactiva los workflows programados si el repositorio queda 60 días sin actividad**.
  Los commits del historial cuentan como actividad, así que en la práctica no pasa; si algún
  día se desactiva, se reactiva con un clic desde la pestaña *Actions*.
- ⚠️ **La suscripción IoT Core de Tuya es de prueba y vence cada mes.** Se renueva en
  iot.tuya.com → *Cloud → Project Management → Upgrade IoT Core Plan → Trial edition*, y
  después el botón **Extend trial period** (pide un cuestionario corto, 1/3/6 meses).
  Si vence, este workflow deja de leer la estación (lo vas a ver como error en los logs).

## Cambiar qué se publica

Editá `mapeo.json`. Cada línea dice: dato del puente ← código de Tuya + escala + unidad de
origen. Los códigos de esta estación, con su significado (los nombres en chino de la
especificación de Tuya traducidos), están documentados en el LEEME del proyecto local:

| Dato | Código Tuya | Escala |
|---|---|---|
| Temperatura exterior | `temp_current_external` | 0,1 |
| Humedad exterior | `humidity_outdoor` | 1 |
| Temperatura interior | `temp_current` | 0,1 |
| Humedad interior | `humidity_value` | 1 |
| Presión | `atmospheric_pressture` | 1 |
| Viento medio | `windspeed_avg` | 0,1 (km/h) |
| Ráfaga | `windspeed_gust` | 0,1 (km/h) |
| Lluvia de hoy | `rain_24h` | 0,1 |
| Intensidad de lluvia | `rain_rate` | 0,1 |
| Índice UV | `uv_index` | 1 |
| Punto de rocío | `dew_point_temp` | 0,1 |

**Falta la dirección del viento**: esta estación no la expone como dato numérico en la nube
(el DP 134 `Wing_direction` viene como binario en base64). Se está investigando; cuando se
descifre, se agrega `"viento_dir"` al mapeo y Windguru empieza a mostrar la flecha.

## Probar cambios sin esperar el cron

```bash
pip install tinytuya requests

TUYA_ACCESS_ID=... TUYA_ACCESS_SECRET=... TUYA_DEVICE_ID=... \
  python sync_tuya.py --simular
```

`--simular` lee la estación, muestra lo que enviaría y guarda el historial, pero **no publica**.
