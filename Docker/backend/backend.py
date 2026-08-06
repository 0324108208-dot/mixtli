import os
import time
import random
import string
import requests
import urllib3
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from proxmoxer import ProxmoxAPI
from dotenv import load_dotenv
from urllib3.exceptions import InsecureRequestWarning

load_dotenv()
urllib3.disable_warnings()

PROXMOX_HOST = os.getenv("PROXMOX_HOST", "")
PROXMOX_USER = os.getenv("PROXMOX_USER", "root@pam")
PROXMOX_PASS = os.getenv("PROXMOX_PASS", "")
PROXMOX_NODE = os.getenv("PROXMOX_NODE", "")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

GUACAMOLE_URL = os.getenv("GUACAMOLE_URL", "http://guacamole:8080/guacamole")
GUACAMOLE_ADMIN_USER = os.getenv("GUACAMOLE_ADMIN_USER", "guacadmin")
GUACAMOLE_ADMIN_PASS = os.getenv("GUACAMOLE_ADMIN_PASS", "")
GUACAMOLE_DATASOURCE = os.getenv("GUACAMOLE_DATASOURCE", "postgresql")

PLANES = {
    "basico": {"ram_mb": 1024, "cores": 1, "disco_gb": 2},
    "intermedio": {"ram_mb": 2048, "cores": 2, "disco_gb": 4},
    "avanzado": {"ram_mb": 4096, "cores": 2, "disco_gb": 6},
}

TEMPLATES = {
    "ubuntu": {
        "vmid": int(os.getenv("TEMPLATE_UBUNTU_ID", "2000")),
        "protocolo": "ssh",
    },
    "windows": {
        "vmid": int(os.getenv("TEMPLATE_WINDOWS_ID", "500")),
        "protocolo": "rdp",
    },
    "cachyos": {
        "vmid": int(os.getenv("TEMPLATE_CACHYOS_ID", "500")),
        "protocolo": "ssh",
    },
}

VMID_RANGO_INICIO = int(os.getenv("VMID_RANGO_INICIO", "1000"))
VMID_RANGO_FIN = int(os.getenv("VMID_RANGO_FIN", "1999"))

px = None

def conectar_proxmox():
    global px
    if not PROXMOX_HOST or not PROXMOX_PASS or not PROXMOX_NODE:
        return None
    try:
        conn = ProxmoxAPI(PROXMOX_HOST, user=PROXMOX_USER, password=PROXMOX_PASS, verify_ssl=False)
        return conn
    except Exception as e:
        return None

px = conectar_proxmox()
app = FastAPI(title="mixtli backend api", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

def check_proxmox():
    global px
    if not px:
        px = conectar_proxmox()
    if not px:
        raise HTTPException(status_code=503, detail="sin conexion a proxmox")

def check_admin(x_admin_key: Optional[str] = Header(default=None)):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="ADMIN_API_KEY no configurada")
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="no autorizado")
    return True

class CloneVM(BaseModel):
    nuevo_id: int
    nombre: str
    template_id: int

class UpdateSpecs(BaseModel):
    vmid: int
    ram_mb: int
    cores: int

class SnapshotCreate(BaseModel):
    vmid: int
    nombre: str
    descripcion: str = ""

class ContratarServicio(BaseModel):
    nombre_cliente: str
    correo: str
    plan: str
    sistema_operativo: str

def notificar_n8n_credenciales(correo: str, usuario: str, llave: str, vm_nombre: str):
    try:
        requests.post(
            "http://localhost:5678/webhook/enviar-credenciales",
            json={"correo": correo, "usuario_acceso": usuario, "llave_acceso": llave, "nodo": vm_nombre},
            timeout=2
        )
    except Exception as e:
        print(f"Error avisando a n8n: {e}")

def guac_login() -> str:
    if not GUACAMOLE_ADMIN_PASS:
        raise HTTPException(status_code=503, detail="GUACAMOLE_ADMIN_PASS no configurada")
    try:
        r = requests.post(
            f"{GUACAMOLE_URL}/api/tokens",
            data={"username": GUACAMOLE_ADMIN_USER, "password": GUACAMOLE_ADMIN_PASS},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["authToken"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"no se pudo autenticar contra guacamole: {e}")

def guac_crear_conexion(token: str, nombre: str, protocolo: str, ip_vm: str, user_vm: str, pass_vm: str, puerto: str) -> str:
    if protocolo == "rdp":
        parametros = {
            "hostname": ip_vm,
            "port": puerto or "3389",
            "username": user_vm,
            "password": pass_vm,
            "security": "any",
            "ignore-cert": "true",
        }
    elif protocolo == "ssh":
        parametros = {
            "hostname": ip_vm,
            "port": puerto or "22",
            "username": user_vm,
            "password": pass_vm,
        }
    else:
        parametros = {
            "hostname": ip_vm,
            "port": puerto or "5900",
            "password": pass_vm,
        }

    body = {
        "parentIdentifier": "ROOT",
        "name": nombre,
        "protocol": protocolo,
        "parameters": parametros,
        "attributes": {"max-connections": "2", "max-connections-per-user": "1"},
    }
    r = requests.post(
        f"{GUACAMOLE_URL}/api/session/data/{GUACAMOLE_DATASOURCE}/connections",
        params={"token": token},
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["identifier"]

def guac_crear_usuario(token: str, username: str, password: str):
    body = {
        "username": username,
        "password": password,
        "attributes": {"disabled": "", "expired": "", "access-window-start": "", "access-window-end": ""},
    }
    r = requests.post(
        f"{GUACAMOLE_URL}/api/session/data/{GUACAMOLE_DATASOURCE}/users",
        params={"token": token},
        json=body,
        timeout=10,
    )
    r.raise_for_status()

def guac_dar_permiso_solo_a_su_conexion(token: str, username: str, connection_id: str):
    body = [{"op": "add", "path": f"/connectionPermissions/{connection_id}", "value": "READ"}]
    r = requests.patch(
        f"{GUACAMOLE_URL}/api/session/data/{GUACAMOLE_DATASOURCE}/users/{username}/permissions",
        params={"token": token},
        json=body,
        timeout=10,
    )
    r.raise_for_status()

def generar_llave_unica(largo: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=largo))

def siguiente_vmid_libre() -> int:
    check_proxmox()
    usados = {v["vmid"] for v in px.nodes(PROXMOX_NODE).qemu.get()}
    for candidato in range(VMID_RANGO_INICIO, VMID_RANGO_FIN + 1):
        if candidato not in usados:
            return candidato
    raise HTTPException(status_code=507, detail="no hay vmid libres en el rango reservado")

def obtener_ip_vm(vmid: int, intentos: int = 15, espera_seg: int = 3) -> Optional[str]:
    check_proxmox()
    for _ in range(intentos):
        try:
            redes = px.nodes(PROXMOX_NODE).qemu(vmid).agent("network-get-interfaces").get()
            lista_redes = redes.get("result", []) if isinstance(redes, dict) else redes
            for iface in lista_redes:
                for dir_ in iface.get("ip-addresses", []):
                    ip = dir_.get("ip-address", "")
                    if dir_.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                        return ip
        except Exception:
            pass
        time.sleep(espera_seg)
    return None

@app.get("/health")
def health():
    return {"status": "ok", "proxmox_conectado": px is not None}

@app.get("/")
def raiz():
    return {"status": "mixtli backend corriendo"}

@app.get("/catalogo")
def catalogo():
    return {"planes": PLANES, "sistemas_operativos": list(TEMPLATES.keys())}

@app.post("/servicios/contratar")
def contratar_servicio(data: ContratarServicio):
    check_proxmox()
    if data.plan not in PLANES:
        raise HTTPException(status_code=400, detail="plan invalido")
    if data.sistema_operativo not in TEMPLATES:
        raise HTTPException(status_code=400, detail="sistema operativo invalido")

    specs = PLANES[data.plan]
    plantilla = TEMPLATES[data.sistema_operativo]
    nuevo_id = siguiente_vmid_libre()
    nombre_vm = f"cliente-{data.nombre_cliente}-{nuevo_id}".lower().replace(" ", "-")

    try:
        px.nodes(PROXMOX_NODE).qemu(plantilla["vmid"]).clone.post(newid=nuevo_id, name=nombre_vm, full=1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"error clonando: {e}")

    # AUMENTÉ ESTO: De 30 a 75 para que le dé 150 segundos de paciencia al Proxmox y no crashee
    for _ in range(75):
        try:
            status = px.nodes(PROXMOX_NODE).qemu(nuevo_id).status.current.get()
            if not status.get("lock"):
                break
        except Exception:
            pass
        time.sleep(2)
    else:
        raise HTTPException(status_code=500, detail="Timeout esperando a que termine de clonar")

    try:
        px.nodes(PROXMOX_NODE).qemu(nuevo_id).config.post(memory=specs["ram_mb"], cores=specs["cores"])
        px.nodes(PROXMOX_NODE).qemu(nuevo_id).status.start.post()
    except Exception as e:
        return {"error": f"FALLO_EXACTO: {str(e)}"}, 500

    ip_vm = obtener_ip_vm(nuevo_id, intentos=25, espera_seg=3)
    usuario_vm = os.getenv(f"TEMPLATE_{data.sistema_operativo.upper()}_USER", "usuario")
    password_vm = os.getenv(f"TEMPLATE_{data.sistema_operativo.upper()}_PASS", "cambiar123")

    resultado = {
        "vmid": nuevo_id,
        "nombre_vm": nombre_vm,
        "plan": data.plan,
        "specs": specs,
        "sistema_operativo": data.sistema_operativo,
        "ip": ip_vm,
    }

    if not ip_vm:
        resultado["estado"] = "aprovisionando"
        resultado["mensaje"] = "llamame despues en generar-acceso"
        return resultado

    acceso = _crear_acceso_guacamole(nuevo_id, nombre_vm, plantilla["protocolo"], ip_vm, usuario_vm, password_vm)
    notificar_n8n_credenciales(data.correo, acceso["usuario_acceso"], acceso["llave_acceso"], nombre_vm)

    resultado["estado"] = "listo"
    resultado.update(acceso)
    return resultado

def _crear_acceso_guacamole(vmid: int, nombre_vm: str, protocolo: str, ip_vm: str, usuario_vm: str, password_vm: str) -> dict:
    token_admin = guac_login()
    sufijo_unico = generar_llave_unica(4)

    connection_id = guac_crear_conexion(
        token_admin,
        nombre=f"{nombre_vm}-{sufijo_unico}",
        protocolo=protocolo,
        ip_vm=ip_vm,
        user_vm=usuario_vm,
        pass_vm=password_vm,
        puerto="3389" if protocolo == "rdp" else ("22" if protocolo == "ssh" else "5900"),
    )

    usuario_acceso = f"cliente{vmid}-{sufijo_unico}"
    llave_acceso = generar_llave_unica()

    guac_crear_usuario(token_admin, usuario_acceso, llave_acceso)
    guac_dar_permiso_solo_a_su_conexion(token_admin, usuario_acceso, connection_id)

    return {
        "usuario_acceso": usuario_acceso,
        "llave_acceso": llave_acceso,
        "url_acceso": f"/guacamole/#/client/{connection_id}",
    }

@app.post("/servicios/{vmid}/generar-acceso")
def generar_acceso(vmid: int, sistema_operativo: str, correo: str):
    if sistema_operativo not in TEMPLATES:
        raise HTTPException(status_code=400, detail="so invalido")

    ip_vm = obtener_ip_vm(vmid, intentos=5, espera_seg=2)
    if not ip_vm:
        raise HTTPException(status_code=409, detail="sin ip aun")

    protocolo = TEMPLATES[sistema_operativo]["protocolo"]
    usuario_vm = os.getenv(f"TEMPLATE_{sistema_operativo.upper()}_USER", "usuario")
    password_vm = os.getenv(f"TEMPLATE_{sistema_operativo.upper()}_PASS", "cambiar123")
    check_proxmox()
    try:
        nombre_vm = px.nodes(PROXMOX_NODE).qemu(vmid).config.get().get("name", f"vm-{vmid}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    acceso = _crear_acceso_guacamole(vmid, nombre_vm, protocolo, ip_vm, usuario_vm, password_vm)
    notificar_n8n_credenciales(correo, acceso["usuario_acceso"], acceso["llave_acceso"], nombre_vm)
    return acceso

@app.get("/vms")
def listar_vms(_: bool = Depends(check_admin)):
    check_proxmox()
    vms = px.nodes(PROXMOX_NODE).qemu.get()
    vms_data = []
    for v in vms:
        vms_data.append({
            "vmid": v.get("vmid"),
            "nombre": v.get("name"),
            "estado": v.get("status"),
            "ram_mb": int(v.get("maxmem", 0) / 1024 / 1024) if v.get("maxmem") else 0,
            "cores": v.get("cpus", 0),
            "disco_gb": int(v.get("maxdisk", 0) / 1024 / 1024 / 1024) if v.get("maxdisk") else 0
        })
    return vms_data

@app.post("/vms/clonar")
def clonar_vm(data: CloneVM, _: bool = Depends(check_admin)):
    check_proxmox()
    px.nodes(PROXMOX_NODE).qemu(data.template_id).clone.post(newid=data.nuevo_id, name=data.nombre, full=1)
    return {"status": "ok"}

@app.post("/vms/{vmid}/encender")
def encender_vm(vmid: int, _: bool = Depends(check_admin)):
    check_proxmox()
    px.nodes(PROXMOX_NODE).qemu(vmid).status.start.post()
    return {"status": "ok"}

@app.post("/vms/{vmid}/apagar")
def apagar_vm(vmid: int, _: bool = Depends(check_admin)):
    check_proxmox()
    px.nodes(PROXMOX_NODE).qemu(vmid).status.shutdown.post()
    return {"status": "ok"}

@app.delete("/vms/{vmid}")
def eliminar_vm(vmid: int, _: bool = Depends(check_admin)):
    check_proxmox()
    px.nodes(PROXMOX_NODE).qemu(vmid).delete()
    return {"status": "ok"}

WAZUH_INDEXER_HOST = os.getenv("WAZUH_INDEXER_HOST", "192.168.100.35")
WAZUH_INDEXER_PORT = os.getenv("WAZUH_INDEXER_PORT", "9200")
WAZUH_INDEXER_USER = os.getenv("WAZUH_INDEXER_USER", "admin")
WAZUH_INDEXER_PASS = os.getenv("WAZUH_INDEXER_PASS", "admin")

def _nivel_a_texto(nivel: int) -> str:
    if nivel >= 10: return "alta"
    if nivel >= 6: return "media"
    return "baja"

def _consultar_wazuh(size: int = 50, fuente: str = None):
    url = f"https://{WAZUH_INDEXER_HOST}:{WAZUH_INDEXER_PORT}/wazuh-alerts-*/_search"
    query = {"size": size, "sort": [{"timestamp": {"order": "desc"}}], "query": {"match_all": {}}}
    try:
        resp = requests.post(url, json=query, auth=(WAZUH_INDEXER_USER, WAZUH_INDEXER_PASS), verify=False, timeout=5)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception as e:
        return []

    alertas = []
    for h in hits:
        src = h.get("_source", {})
        rule = src.get("rule", {})
        data = src.get("data", {}) or {}
        decoder = src.get("decoder", {}) or {}

        es_suricata = "suricata" in str(decoder).lower() or "suricata" in rule.get("groups", [])

        if es_suricata and "alert" in data:
            detalle = f"[Regla {rule.get('id', 'N/A')}] {data['alert'].get('signature', rule.get('description', ''))}"
        else:
            detalle = f"[Regla {rule.get('id', 'N/A')}] {rule.get('description', 'Sin descripción')}"

        nivel = rule.get("level", 0)

        alertas.append({
            "id": h.get("_id"),
            "fuente": "suricata" if es_suricata else "wazuh",
            "timestamp": src.get("timestamp") or src.get("@timestamp"),
            "severidad": nivel,
            "severidad_texto": _nivel_a_texto(nivel),
            "firma": detalle,
            "ip_origen": data.get("srcip") or data.get("src_ip", "Local"),
            "ip_destino": data.get("dstip") or data.get("dest_ip", "-"),
            "agente": (src.get("agent", {}) or {}).get("name", "N/A"),
        })

    if fuente:  
        alertas = [a for a in alertas if a["fuente"].lower() == fuente.lower()]
    return alertas

@app.get("/alertas")
def listar_alertas(limite: int = 20, fuente: str = None):
    return _consultar_wazuh(size=max(limite, 50), fuente=fuente)[:limite]

@app.get("/alertas/resumen")
def resumen_alertas():
    alertas = _consultar_wazuh(size=200)
    return {
        "total": len(alertas),
        "altas": len([a for a in alertas if a["severidad_texto"] == "alta"]),
        "medias": len([a for a in alertas if a["severidad_texto"] == "media"]),
        "bajas": len([a for a in alertas if a["severidad_texto"] == "baja"]),
        "suricata": len([a for a in alertas if a["fuente"] == "suricata"]),
        "wazuh": len([a for a in alertas if a["fuente"] == "wazuh"]),
    }
