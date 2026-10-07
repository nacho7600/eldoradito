#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_tuya.py
============
Lee la estacion meteorologica Tuya (iot.tuya.com) y publica en Windguru y/o
Weathercloud. Pensado para correr en GitHub Actions con un cron: no necesita PC.

Las credenciales van SIEMPRE por variables de entorno (nunca en el codigo):

    TUYA_REGION            us (Western America) por defecto
    TUYA_ACCESS_ID         Access ID / Client ID del proyecto
    TUYA_ACCESS_SECRET     Access Secret / Client Secret
    TUYA_DEVICE_ID         Device ID de la estacion
    WINDGURU_UID           uid de la estacion en Windguru ("Other (upload API)")
    WINDGURU_PASSWORD      password de esa estacion
    WEATHERCLOUD_WID       wid del dispositivo en Weathercloud (opcional)
    WEATHERCLOUD_KEY       key de ese dispositivo (opcional)

Opcionales: ESTACION (nombre), MAX_ANTIGUEDAD_MIN (90), HISTORIAL (data/historial.csv)

Uso:
    python sync_tuya.py              lee y publica
    python sync_tuya.py --simular    lee, muestra y guarda, pero NO publica
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import tinytuya

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Mapeo de los codigos de Tuya a los datos del puente. Se puede editar sin tocar
# el codigo creando mapeo.json al lado de este archivo.
# ---------------------------------------------------------------------------
MAPEO_POR_DEFECTO = {
    "temperatura": {"code": "temp_current_external", "escala": 0.1, "unidad_origen": "c"},
    "humedad": {"code": "humidity_outdoor", "escala": 1},
    "temperatura_interior": {"code": "temp_current", "escala": 0.1, "unidad_origen": "c"},
    "humedad_interior": {"code": "humidity_value", "escala": 1},
    "presion": {"code": "atmospheric_pressture", "escala": 1, "unidad_origen": "hpa"},
    "viento_kmh": {"code": "windspeed_avg", "escala": 0.1, "unidad_origen": "km/h"},
    "rafaga_kmh": {"code": "windspeed_gust", "escala": 0.1, "unidad_origen": "km/h"},
    "lluvia_mm": {"code": "rain_24h", "escala": 0.1, "unidad_origen": "mm"},
    "lluvia_tasa_mmh": {"code": "rain_rate", "escala": 0.1, "unidad_origen": "mm"},
    "uv": {"code": "uv_index", "escala": 1},
    "punto_rocio": {"code": "dew_point_temp", "escala": 0.1, "unidad_origen": "c"},
}

A_KMH = {
    "": 1.0, "kmh": 1.0, "km/h": 1.0, "kph": 1.0, "ms": 3.6, "m/s": 3.6, "mps": 3.6,
    "mph": 1.609344, "kt": 1.852, "kn": 1.852, "kts": 1.852, "nudos": 1.852,
}
A_HPA = {"": 1.0, "hpa": 1.0, "mbar": 1.0, "mb": 1.0, "pa": 0.01, "inhg": 33.8639, "mmhg": 1.33322}
A_MM = {"": 1.0, "mm": 1.0, "in": 25.4, "pulg": 25.4}
FAMILIA = {
    "temperatura": "temperatura", "temperatura_2": "temperatura",
    "temperatura_interior": "temperatura", "punto_rocio": "temperatura", "sensacion": "temperatura",
    "humedad": "humedad", "humedad_2": "humedad", "humedad_interior": "humedad",
    "presion": "presion", "viento_kmh": "viento", "rafaga_kmh": "viento",
    "viento_dir": "direccion", "lluvia_mm": "lluvia", "lluvia_tasa_mmh": "lluvia",
    "uv": "uv", "radiacion": "radiacion", "bateria": "bateria",
}
LIMITES_WC = {
    "temp": (-400, 600), "tempin": (-400, 600), "dew": (-400, 600),
    "hum": (0, 100), "humin": (0, 100), "bar": (9000, 11000),
    "wspd": (0, 600), "wspdavg": (0, 600), "wspdhi": (0, 600),
    "wdir": (0, 359), "wdiravg": (0, 359), "wdirhi": (0, 359),
    "rain": (0, 10000), "rainrate": (0, 1000), "uvi": (0, 160), "solarrad": (0, 20000),
}


def aviso(mensaje):
    print("::warning::%s" % mensaje)
    print("  AVISO: %s" % mensaje)


def error(mensaje):
    print("::error::%s" % mensaje)
    print("  ERROR: %s" % mensaje)


def convertir(campo, bruto, entrada):
    valor = float(bruto) * float(entrada.get("escala", 1)) + float(entrada.get("sumar", 0))
    origen = str(entrada.get("unidad_origen", "")).strip().lower()
    familia = FAMILIA.get(campo)
    if familia == "viento":
        valor *= A_KMH.get(origen, 1.0)
    elif familia == "presion":
        valor *= A_HPA.get(origen, 1.0)
    elif familia == "lluvia":
        valor *= A_MM.get(origen, 1.0)
    elif familia == "temperatura" and origen in ("f", "fahrenheit", "°f"):
        valor = (valor - 32.0) * 5.0 / 9.0
    return valor


def traducir(valores, mapeo):
    datos, avisos = {}, []
    for campo, entrada in mapeo.items():
        codigo = entrada.get("code")
        if codigo not in valores:
            avisos.append("%s: la estacion no reporta '%s'" % (campo, codigo))
            continue
        try:
            datos[campo] = convertir(campo, valores[codigo], entrada)
        except (TypeError, ValueError):
            avisos.append("%s ('%s'): valor no numerico %r" % (campo, codigo, valores[codigo]))
    return datos, avisos


def leer_estado(estacion, device_id):
    """Valores actuales y hora real de la ultima medicion (endpoint shadow de Tuya)."""
    try:
        respuesta = estacion.cloudrequest("/v2.0/cloud/thing/%s/shadow/properties" % device_id)
        if isinstance(respuesta, dict) and respuesta.get("success"):
            propiedades = (respuesta.get("result") or {}).get("properties") or []
            valores, momento = {}, None
            for propiedad in propiedades:
                codigo = propiedad.get("code")
                if not codigo:
                    continue
                valores[str(codigo)] = propiedad.get("value")
                sello = propiedad.get("time")
                if sello:
                    instante = datetime.fromtimestamp(int(sello) / 1000.0, tz=timezone.utc)
                    if momento is None or instante > momento:
                        momento = instante
            if valores:
                return valores, momento
    except Exception as falla:                       # noqa: BLE001
        aviso("no pude usar el endpoint shadow (%s), pruebo con el estado simple" % falla)

    respuesta = estacion.getstatus(device_id)
    if not isinstance(respuesta, dict) or not respuesta.get("success"):
        raise SystemExit("Tuya respondio con error al pedir el estado: %s"
                         % json.dumps(respuesta, ensure_ascii=False)[:300])
    resultado = respuesta.get("result")
    if isinstance(resultado, list):
        return {str(x.get("code")): x.get("value") for x in resultado if isinstance(x, dict)}, None
    return (dict(resultado) if isinstance(resultado, dict) else {}), None


def params_weathercloud(datos):
    parametros, avisos = {}, []

    def meter(nombre, valor, factor=1.0):
        numero = int(round(valor * factor))
        limite = LIMITES_WC.get(nombre)
        if limite and not (limite[0] <= numero <= limite[1]):
            avisos.append("%s=%s fuera de rango %s" % (nombre, numero, limite))
            numero = max(limite[0], min(limite[1], numero))
        parametros[nombre] = numero

    if "temperatura" in datos:
        meter("temp", datos["temperatura"], 10)
    if "temperatura_interior" in datos:
        meter("tempin", datos["temperatura_interior"], 10)
    if "punto_rocio" in datos:
        meter("dew", datos["punto_rocio"], 10)
    if "humedad" in datos:
        meter("hum", datos["humedad"])
    if "humedad_interior" in datos:
        meter("humin", datos["humedad_interior"])
    if "presion" in datos:
        meter("bar", datos["presion"], 10)
    if "viento_kmh" in datos:
        meter("wspdavg", datos["viento_kmh"] / 3.6, 10)
    if "rafaga_kmh" in datos:
        meter("wspdhi", datos["rafaga_kmh"] / 3.6, 10)
    if "viento_dir" in datos:
        meter("wdiravg", datos["viento_dir"])
    if "lluvia_mm" in datos:
        meter("rain", datos["lluvia_mm"], 10)
    if "lluvia_tasa_mmh" in datos:
        meter("rainrate", datos["lluvia_tasa_mmh"], 10)
    if "uv" in datos:
        meter("uvi", datos["uv"], 10)
    parametros["software"] = "puente_tuya_github_1.0"
    return parametros, avisos


def params_windguru(datos, momento=None):
    parametros = {}
    if momento is not None:
        parametros["unixtime"] = int(momento.timestamp())
    if "viento_kmh" in datos:
        parametros["wind_avg"] = round(datos["viento_kmh"] / 1.852, 1)
    if "rafaga_kmh" in datos:
        parametros["wind_max"] = round(datos["rafaga_kmh"] / 1.852, 1)
    if "viento_dir" in datos:
        parametros["wind_direction"] = int(round(datos["viento_dir"])) % 360
    if "temperatura" in datos:
        parametros["temperature"] = round(datos["temperatura"], 1)
    if "humedad" in datos:
        parametros["rh"] = int(round(datos["humedad"]))
    if "presion" in datos:
        parametros["mslp"] = round(datos["presion"], 1)
    if "lluvia_mm" in datos:
        parametros["precip"] = round(datos["lluvia_mm"], 1)
    return parametros


def publicar_weathercloud(datos, wid, key, simular):
    extra, avisos = params_weathercloud(datos)
    for texto in avisos:
        aviso("Weathercloud: %s" % texto)
    if simular:
        return True, "simulado: %s" % json.dumps(extra, ensure_ascii=False)
    try:
        respuesta = requests.get("https://api.weathercloud.net/v01/set",
                                 params={"wid": wid, "key": key, **extra}, timeout=25)
    except requests.RequestException as falla:
        return False, "error de red: %s" % falla
    cuerpo = (respuesta.text or "").strip()
    ok = respuesta.status_code == 200 and "error" not in cuerpo.lower()
    return ok, "HTTP %s %s" % (respuesta.status_code, cuerpo[:160])


def publicar_windguru(datos, uid, password, simular, momento=None):
    extra = params_windguru(datos, momento)
    if not extra:
        return False, "no hay ningun dato que Windguru acepte"
    if simular:
        return True, "simulado: %s" % json.dumps(extra, ensure_ascii=False)
    sal = str(int(datetime.now(timezone.utc).timestamp()))
    firma = hashlib.md5((sal + uid + password).encode("utf-8")).hexdigest()
    try:
        respuesta = requests.get("http://www.windguru.cz/upload/api.php",
                                 params={"uid": uid, "salt": sal, "hash": firma, **extra}, timeout=25)
    except requests.RequestException as falla:
        return False, "error de red: %s" % falla
    cuerpo = (respuesta.text or "").strip()
    # OJO: Windguru contesta HTTP 200 aunque rechace el envio (el error viene en el cuerpo),
    # asi que hay que mirar el texto: exito = "OK", rechazo = "ERROR ...".
    minusculas = cuerpo.lower()
    ok = respuesta.status_code == 200 and "ok" in minusculas and "error" not in minusculas
    return ok, "HTTP %s %s" % (respuesta.status_code, cuerpo[:160])


def guardar_historial(ruta, datos, momento):
    if not ruta:
        return
    if not os.path.isabs(ruta):
        ruta = os.path.join(BASE, ruta)
    carpeta = os.path.dirname(ruta)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    campos = ["fecha_utc"] + sorted(datos.keys())
    nuevo = not os.path.exists(ruta)
    with open(ruta, "a", newline="", encoding="utf-8") as archivo:
        escritor = csv.writer(archivo)
        if nuevo:
            escritor.writerow(campos)
        fila = {"fecha_utc": (momento or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")}
        fila.update({clave: round(valor, 2) for clave, valor in datos.items()})
        escritor.writerow([fila.get(campo, "") for campo in campos])


def leer_json(ruta, por_defecto=None):
    try:
        with open(ruta, encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, ValueError):
        return por_defecto


def guardar_json(ruta, contenido):
    carpeta = os.path.dirname(ruta)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as archivo:
        json.dump(contenido, archivo, ensure_ascii=False, indent=2)


def traer_eventos(estacion, device_id, desde_ms, hasta_ms, maximo=60):
    """Trae del registro de Tuya todos los reportes de datos ocurridos en el rango.

    Cada evento es un dato que CAMBIO, con su hora real (event_time, en milisegundos).
    """
    eventos, inicio, consultas = [], None, 0
    while consultas < maximo:
        consulta = {
            "start_time": desde_ms,
            "end_time": hasta_ms,
            "type": "7",                 # 7 = reporte de datos (DP report)
            "size": 100,
            "query_type": 1,
        }
        if inicio:
            consulta["start_row_key"] = inicio
        respuesta = estacion.cloudrequest("/v1.0/devices/%s/logs" % device_id, query=consulta)
        consultas += 1
        if not isinstance(respuesta, dict) or not respuesta.get("success"):
            raise RuntimeError("Tuya respondio con error al pedir el registro: %s"
                               % json.dumps(respuesta, ensure_ascii=False)[:250])
        resultado = respuesta.get("result") or {}
        eventos += resultado.get("logs") or []
        if resultado.get("has_next") and resultado.get("next_row_key"):
            inicio = resultado["next_row_key"]
            time.sleep(2)               # pausa entre paginas: Tuya corta si se consulta muy seguido
        else:
            break
    return eventos


def armar_filas_eventos(eventos, estado, mapeo):
    """Reconstruye el estado completo en cada momento en que cambio algo.

    `estado` se modifica: queda con el ultimo valor conocido de cada codigo.
    """
    filas = []
    for evento in sorted(eventos, key=lambda x: x.get("event_time") or 0):
        codigo = evento.get("code")
        sello = evento.get("event_time")
        if not codigo or sello is None:
            continue
        estado[str(codigo)] = evento.get("value")
        momento = datetime.fromtimestamp(int(sello) / 1000.0, tz=timezone.utc)
        filas.append({
            "fecha_utc": momento.strftime("%Y-%m-%d %H:%M:%S"),
            "cambio": str(codigo),
            "datos": dict(traducir(estado, mapeo)[0]),
        })
    return filas


def ruta_por_dia(ruta_base, dia):
    """data/historial_completo.csv -> data/historial_completo_2026-10-07.csv"""
    raiz, extension = os.path.splitext(ruta_base)
    return "%s_%s%s" % (raiz, dia, extension or ".csv")


def fecha_mas_vieja(ruta_base):
    """Fecha (en ms) del registro mas viejo ya guardado en los archivos de historial completo.

    Sirve para saber desde donde seguir bajando cuando el estado interno no lo dice.
    """
    carpeta = os.path.dirname(ruta_base) or "."
    prefijo = os.path.splitext(os.path.basename(ruta_base))[0]
    if not os.path.isdir(carpeta):
        return None
    mas_vieja = None
    for nombre in sorted(os.listdir(carpeta)):
        if not nombre.startswith(prefijo) or not nombre.endswith(".csv"):
            continue
        try:
            with open(os.path.join(carpeta, nombre), encoding="utf-8") as archivo:
                for linea in archivo:
                    if not linea.strip() or linea.startswith("fecha_utc"):
                        continue
                    fecha = linea.split(",")[0]
                    if mas_vieja is None or fecha < mas_vieja:
                        mas_vieja = fecha
        except OSError:
            continue
    if not mas_vieja:
        return None
    try:
        momento = datetime.strptime(mas_vieja, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(momento.timestamp() * 1000)
    except ValueError:
        return None


def completar_historial(estacion, device_id, mapeo, valores_actuales, ruta_csv, ruta_estado,
                        horas_iniciales, minutos_entre, bloque_horas, bloques_max):
    """Agrega al CSV completo todas las mediciones que pasaron desde la ultima corrida.

    Tuya tiene dos limites que condicionan esto:
      * la consulta del registro no se puede hacer muy seguido ("The log query is too
        frequent"), asi que se consulta como maximo una vez cada `minutos_entre` minutos;
      * una ventana grande NO devuelve todo: corta y deja afuera los registros mas viejos.
        Por eso se consulta en bloques de `bloque_horas` horas, y si sobra presupuesto se
        sigue hacia atras para recuperar lo mas viejo que Tuya conserve.
    """
    guardado = leer_json(ruta_estado, {}) or {}
    estado = dict(guardado.get("estado") or {})
    ultimo_ms = guardado.get("ultimo_ms")
    cubierto_desde = guardado.get("cubierto_desde_ms")
    intento_ms = guardado.get("intento_ms")

    ahora = datetime.now(timezone.utc)
    ahora_ms = int(ahora.timestamp() * 1000)

    if intento_ms and (ahora_ms - int(intento_ms)) < minutos_entre * 60 * 1000:
        print("Salteo la consulta del registro de Tuya: la anterior fue hace %.0f min y se"
              " consulta cada %.0f min para no chocar con el limite."
              % ((ahora_ms - int(intento_ms)) / 60000.0, minutos_entre))
        return 0

    bloque_ms = int(bloque_horas * 3600 * 1000)
    hasta_ms = ahora_ms
    if ultimo_ms:
        desde_ms = int(ultimo_ms)
    else:
        desde_ms = int((ahora - timedelta(hours=horas_iniciales)).timestamp() * 1000)

    # Tuya NO devuelve todos los registros de una ventana grande: si se pide mucho, corta y
    # deja afuera los mas viejos. Por eso se consulta en bloques chicos, priorizando lo mas
    # reciente, y si sobra presupuesto se sigue hacia atras.
    filas = []
    usados = 0
    total_eventos = 0
    error_limite = None

    ventana = bloques_max * bloque_ms
    cursor = max(desde_ms, hasta_ms - ventana)
    # `pendiente` es el punto mas viejo que ya tenemos cubierto: desde ahi se sigue bajando.
    # Si el estado no lo dice, se deduce del registro mas viejo que haya en los archivos.
    pendiente = int(cubierto_desde) if cubierto_desde else (fecha_mas_vieja(ruta_csv) or cursor)

    while cursor < hasta_ms and usados < bloques_max:
        fin = min(cursor + bloque_ms, hasta_ms)
        try:
            eventos = traer_eventos(estacion, device_id, cursor, fin)
        except Exception as falla:                          # noqa: BLE001
            error_limite = falla
            break
        usados += 1
        total_eventos += len(eventos)
        filas += armar_filas_eventos(eventos, estado, mapeo)
        print("  bloque %s -> %s: %d eventos" % (
            datetime.fromtimestamp(cursor / 1000.0, tz=timezone.utc).strftime("%d %H:%M"),
            datetime.fromtimestamp(fin / 1000.0, tz=timezone.utc).strftime("%d %H:%M"), len(eventos)))
        cursor = fin
        if cursor < hasta_ms:
            time.sleep(3)          # respiro entre bloques: Tuya corta si se consulta muy seguido

    while usados < bloques_max and pendiente > 0:
        fin = pendiente
        inicio_bloque = max(0, fin - bloque_ms)
        try:
            eventos = traer_eventos(estacion, device_id, inicio_bloque, fin)
        except Exception as falla:                          # noqa: BLE001
            error_limite = falla
            break
        usados += 1
        if not eventos:
            print("  hacia atras: Tuya no guarda nada antes de %s"
                  % datetime.fromtimestamp(fin / 1000.0, tz=timezone.utc).strftime("%d %H:%M"))
            pendiente = 0
            break
        total_eventos += len(eventos)
        # Cada bloque viejo arma su propio estado (son datos anteriores a lo que ya tenemos)
        filas += armar_filas_eventos(eventos, {}, mapeo)
        print("  hacia atras %s -> %s: %d eventos" % (
            datetime.fromtimestamp(inicio_bloque / 1000.0, tz=timezone.utc).strftime("%d %H:%M"),
            datetime.fromtimestamp(fin / 1000.0, tz=timezone.utc).strftime("%d %H:%M"), len(eventos)))
        pendiente = inicio_bloque
        time.sleep(3)

    print("Eventos en total: %d" % total_eventos)
    if error_limite:
        aviso("Tuya corto la consulta del registro (%s): la proxima corrida sigue donde quedo"
              % error_limite)

    # La lectura actual va siempre como ultima fila, asi el archivo termina en el presente
    estado.update({str(k): v for k, v in (valores_actuales or {}).items()})
    datos_ahora, _ = traducir(estado, mapeo)
    filas.append({
        "fecha_utc": ahora.strftime("%Y-%m-%d %H:%M:%S"),
        "cambio": "consulta",
        "datos": datos_ahora,
    })

    campos = ["fecha_utc", "cambio"] + sorted(datos_ahora.keys())
    escritas = 0
    por_dia = {}
    for fila in filas:
        por_dia.setdefault(fila["fecha_utc"][:10], []).append(fila)

    for dia in sorted(por_dia):
        ruta_csv_dia = ruta_por_dia(ruta_csv, dia)
        carpeta = os.path.dirname(ruta_csv_dia)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        nuevo = not os.path.exists(ruta_csv_dia)
        ultima = None
        if not nuevo:
            with open(ruta_csv_dia, encoding="utf-8") as archivo:
                for linea in archivo:
                    if linea.strip():
                        ultima = linea.split(",")[0]
        with open(ruta_csv_dia, "a", newline="", encoding="utf-8") as archivo:
            escritor = csv.writer(archivo)
            if nuevo:
                escritor.writerow(campos)
            for fila in por_dia[dia]:
                if ultima and fila["fecha_utc"] <= ultima:
                    continue
                escritor.writerow([fila["fecha_utc"], fila["cambio"]]
                                  + [(round(fila["datos"][campo], 2) if campo in fila["datos"] else "")
                                     for campo in sorted(datos_ahora.keys())])
                ultima = fila["fecha_utc"]
                escritas += 1

    guardar_json(ruta_estado, {"ultimo_ms": int(max(cursor, ultimo_ms or 0)),
                               "cubierto_desde_ms": int(pendiente),
                               "intento_ms": ahora_ms, "estado": estado})
    print("Historial completo: %d filas nuevas en %d archivo(s) del dia" % (escritas, len(por_dia)))
    return escritas


def main():
    analizador = argparse.ArgumentParser(description="Puente Tuya -> Windguru / Weathercloud para GitHub Actions.")
    analizador.add_argument("--simular", action="store_true", help="leer y mostrar, sin publicar")
    argumentos = analizador.parse_args()

    region = os.environ.get("TUYA_REGION", "us")
    access_id = os.environ.get("TUYA_ACCESS_ID", "")
    access_secret = os.environ.get("TUYA_ACCESS_SECRET", "")
    device_id = os.environ.get("TUYA_DEVICE_ID", "")
    windguru_uid = os.environ.get("WINDGURU_UID", "")
    windguru_password = os.environ.get("WINDGURU_PASSWORD", "")
    weathercloud_wid = os.environ.get("WEATHERCLOUD_WID", "")
    weathercloud_key = os.environ.get("WEATHERCLOUD_KEY", "")
    max_antiguedad = float(os.environ.get("MAX_ANTIGUEDAD_MIN", "90"))
    historial = os.environ.get("HISTORIAL", "data/historial.csv")
    historial_completo = os.environ.get("HISTORIAL_COMPLETO", "data/historial_completo.csv")
    estado_crudo = os.environ.get("ESTADO_CRUDO", "data/estado_crudo.json")
    horas_iniciales = float(os.environ.get("BACKFILL_HORAS", "24"))
    minutos_entre = float(os.environ.get("BACKFILL_MINUTOS", "30"))
    bloque_horas = float(os.environ.get("BACKFILL_BLOQUE_HORAS", "2"))
    bloques_max = int(os.environ.get("BACKFILL_BLOQUES", "4"))
    estacion_nombre = os.environ.get("ESTACION", "Estacion Tuya")
    simular = argumentos.simular or os.environ.get("SIMULAR", "") in ("1", "true", "si", "sí")

    faltan = [nombre for nombre, valor in (
        ("TUYA_ACCESS_ID", access_id), ("TUYA_ACCESS_SECRET", access_secret),
        ("TUYA_DEVICE_ID", device_id)) if not valor]
    if faltan:
        error("faltan secretos: %s" % ", ".join(faltan))
        return 2
    if not windguru_uid and not weathercloud_wid:
        aviso("no hay destino configurado (WINDGURU_UID o WEATHERCLOUD_WID): solo se guarda el historial")

    archivo_mapeo = os.path.join(BASE, "mapeo.json")
    mapeo = MAPEO_POR_DEFECTO
    if os.path.exists(archivo_mapeo):
        with open(archivo_mapeo, encoding="utf-8") as archivo:
            mapeo = json.load(archivo).get("mapeo", MAPEO_POR_DEFECTO)

    print("Estacion: %s (%s) - region %s" % (estacion_nombre, device_id, region))
    estacion = tinytuya.Cloud(apiRegion=region, apiKey=access_id, apiSecret=access_secret)
    if not estacion.token:
        error("Tuya rechazo las credenciales: %s" % json.dumps(estacion.error, ensure_ascii=False))
        return 2

    valores, momento = leer_estado(estacion, device_id)
    if not valores:
        error("Tuya no devolvio ningun valor")
        return 1

    datos, avisos = traducir(valores, mapeo)
    for texto in avisos:
        aviso(texto)
    if not datos:
        error("no pude traducir ningun dato: revisar mapeo.json")
        return 1

    print("Datos: %s" % json.dumps({k: round(v, 1) for k, v in datos.items()}, ensure_ascii=False))
    if momento is not None:
        edad = (datetime.now(timezone.utc) - momento).total_seconds() / 60.0
        print("Ultima medicion: %s (hace %.0f min)" % (momento.astimezone().isoformat(timespec="seconds"),
                                                       max(0.0, edad)))
        if edad > max_antiguedad:
            aviso("la ultima medicion tiene %.0f min: no se publica (limite %.0f)" % (edad, max_antiguedad))
            guardar_historial(historial, datos, momento)
            return 0

    publicados = 0
    if windguru_uid:
        ok, detalle = publicar_windguru(datos, windguru_uid, windguru_password, simular, momento)
        print("Windguru: %s (%s)" % ("OK" if ok else "FALLO", detalle))
        if not ok:
            aviso("Windguru: %s" % detalle)
        publicados += 1 if ok else 0
    if weathercloud_wid:
        ok, detalle = publicar_weathercloud(datos, weathercloud_wid, weathercloud_key, simular)
        print("Weathercloud: %s (%s)" % ("OK" if ok else "FALLO", detalle))
        if not ok:
            aviso("Weathercloud: %s" % detalle)
        publicados += 1 if ok else 0

    guardar_historial(historial, datos, momento)

    # Relleno: trae del registro de Tuya todas las mediciones desde la corrida anterior,
    # asi el historial del repo queda completo aunque GitHub saltee corridas.
    if historial_completo:
        try:
            completar_historial(estacion, device_id, mapeo, valores,
                                historial_completo, estado_crudo, horas_iniciales, minutos_entre,
                                bloque_horas, bloques_max)
        except Exception as falla:                          # noqa: BLE001
            aviso("no pude completar el historial: %s" % falla)

    print("Listo. Destinos publicados: %d%s" % (publicados, " (simulado)" if simular else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
