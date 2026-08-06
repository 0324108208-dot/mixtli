# Mixtli — backend + frontend + Guacamole con Docker Compose

## Qué cambió respecto a la versión anterior

La pieza que faltaba era cómo el cliente final entra a **su** VM sin ver el
panel de administración ni las VMs de otros clientes. Eso es exactamente lo
que resuelve **Apache Guacamole**: es un gateway que da acceso remoto
(VNC/RDP/SSH) por navegador, y su sistema de permisos nativo permite crear un
usuario que solo puede ver **una** conexión específica — nada más. No hay que
inventar un mecanismo de aislamiento nuevo, Guacamole ya lo trae.

## Estructura

```
mixtli-deploy/
├── docker-compose.yml
├── .env.example          <- copia a .env y llena tus datos reales
├── setup.sh               <- correr UNA vez antes del primer "docker compose up"
├── init/
│   └── 01-initdb.sql      <- lo genera setup.sh, no se escribe a mano
├── backend/
│   ├── backend.py
│   ├── requirements.txt
│   └── Dockerfile
└── frontend/
    ├── index.html          <- tu landing (mixtli-landing.html)
    ├── acceso.html         <- página nueva: aquí el cliente mete su llave y entra a su VM
    ├── nginx.conf
    └── Dockerfile
```

Servicios que corren en Docker (ver `docker-compose.yml`):

| Servicio | Qué hace | ¿Expuesto a internet? |
|---|---|---|
| `guac-postgres` | Base de datos de Guacamole (usuarios, conexiones, permisos) | No |
| `guacd` | Proceso nativo que habla VNC/RDP con cada VM | No |
| `guacamole` | API + motor del gateway HTML5 | No (solo vía nginx) |
| `backend` | FastAPI + proxmoxer + cliente de la API de Guacamole | Puerto 8000 (quitable) |
| `frontend` | nginx: sirve el landing, la página de acceso, y hace de proxy hacia backend y guacamole | Puerto 8080 |

Solo `frontend` necesita quedar accesible desde fuera. Todo lo demás vive
detrás, en la red interna de Docker (`mixtli-net`).

## Cómo levantarlo (primera vez)

### 1. Preparar las plantillas de VM en Proxmox

Antes de tocar Docker, deben existir en Proxmox tres VMs plantilla, **apagadas**,
una por cada sistema operativo que se va a ofrecer (Ubuntu, Windows, CachyOS o
las que se decidan). Cada plantilla debe tener:

- El sistema operativo instalado y con un usuario ya creado (ese usuario/contraseña
  es el que se pone en `.env` como `TEMPLATE_<SO>_USER` / `TEMPLATE_<SO>_PASS`).
- El **agente de QEMU** instalado y habilitado dentro del sistema operativo
  (`qemu-guest-agent` en Linux, el driver correspondiente en Windows). Sin esto
  el backend no puede leer la IP de la VM después de clonarla y encenderla.
- En Windows, el Escritorio Remoto (RDP) habilitado. En Linux, un servidor VNC
  corriendo (o adaptar `backend.py` para usar SSH si se prefiere).
- Anotar el **vmid** de cada plantilla — va en `TEMPLATE_UBUNTU_ID`,
  `TEMPLATE_WINDOWS_ID`, `TEMPLATE_CACHYOS_ID` del `.env`.

### 2. Generar el esquema de Guacamole

```bash
chmod +x setup.sh
./setup.sh
```

Esto descarga (una sola vez) el generador de esquema desde la propia imagen de
Guacamole y lo guarda en `init/01-initdb.sql`. Postgres lo ejecuta solo la
primera vez que arranca.

### 3. Configurar variables de entorno

```bash
cp .env.example .env
nano .env
```

Llenar como mínimo: `PROXMOX_HOST/USER/PASS/NODE`, `ADMIN_API_KEY`,
`TEMPLATE_*_ID`, `TEMPLATE_*_USER/PASS`, `GUACAMOLE_ADMIN_PASS`,
`GUACAMOLE_DB_PASS`.

### 4. Levantar todo

```bash
docker compose up -d --build
```

La primera vez tarda un poco más porque Postgres carga el esquema de
Guacamole. Verificar que todo esté sano:

```bash
docker compose ps
docker compose logs -f guacamole
```

### 5. Cambiar la contraseña de administrador de Guacamole

Entrar a `http://IP_DEL_HOST:8080/guacamole/` con `guacadmin` / `guacadmin`
(usuario y contraseña por defecto de Guacamole) y cambiarla de inmediato desde
**Settings → Preferences**. Esa nueva contraseña es la que va en
`GUACAMOLE_ADMIN_PASS` del `.env` (y luego se reinicia el backend:
`docker compose restart backend`).

## Flujo completo de un cliente contratando un servicio

1. El cliente llena un formulario (en el landing o donde se decida) con su
   nombre, el plan que quiere y el sistema operativo.
2. El frontend llama a `POST /api/servicios/contratar` con esos datos.
3. El backend:
   - Clona la plantilla del SO elegido con un `vmid` nuevo.
   - Le aplica la RAM y los cores del plan contratado.
   - Enciende la VM.
   - Espera a que el agente de QEMU reporte su IP.
   - Crea una conexión en Guacamole apuntando a esa IP (VNC o RDP según el SO).
   - Crea un usuario de Guacamole nuevo, con una contraseña aleatoria — esa es
     la **llave única** del cliente.
   - Le da permiso a ese usuario **solo** sobre su conexión, nada más.
4. El backend devuelve `usuario_acceso` y `llave_acceso` — eso es lo que se le
   entrega al cliente (por correo, en pantalla al terminar la compra, etc).
5. El cliente entra a `acceso.html`, mete su usuario y llave, y cae directo a
   la sesión gráfica de **su** VM. No hay forma de que vea el panel de admin ni
   las VMs de otros, porque su usuario de Guacamole no tiene permiso sobre
   nada más que su propia conexión.

Si al momento de contratar la VM tarda en entregar su IP (sistemas operativos
lentos para arrancar, como Windows), el endpoint devuelve
`"estado": "aprovisionando"` y hay que llamar después a
`POST /servicios/{vmid}/generar-acceso?sistema_operativo=<so>` para terminar
de crear el acceso.

## Catálogo de planes

Definido en `backend.py`, diccionario `PLANES`:

```python
PLANES = {
    "basico":     {"ram_mb": 4096,  "cores": 4, "disco_gb": 40},
    "intermedio": {"ram_mb": 8192,  "cores": 6, "disco_gb": 80},
    "avanzado":   {"ram_mb": 16384, "cores": 8, "disco_gb": 120},
}
```

Ajustar valores o agregar planes nuevos ahí directamente. El frontend puede
consultar `GET /api/catalogo` para pintar las opciones sin tener que
hardcodearlas en el HTML.

Nota: el disco (`disco_gb`) queda documentado en el catálogo pero **no se
redimensiona automáticamente** al clonar — cada plantilla ya trae su tamaño de
disco fijo. Si se necesita variar el disco por plan, se puede agregar una
llamada a `resize` sobre el disco después del clonado (endpoint de Proxmox:
`qemu/{vmid}/resize`), déjalo como mejora futura si hace falta.

## Endpoints de administración (protegidos)

Los endpoints que gestionan VMs directamente (`/vms`, `/vms/clonar`,
`/vms/{vmid}/encender`, etc.) requieren el header `X-Admin-Key` con el valor
de `ADMIN_API_KEY`. Estos son para el panel interno del equipo, el cliente
final nunca los usa — su único punto de entrada es `/servicios/contratar` y
`acceso.html`.

## Seguridad — puntos ya cubiertos y pendientes

Ya cubierto con esta arquitectura:
- El cliente nunca ve el panel de Proxmox ni el de administración del backend.
- Cada cliente solo tiene permiso sobre su propia conexión en Guacamole.
- La base de datos de Guacamole y `guacd` no están expuestos fuera de la red
  interna de Docker.

Pendiente / recomendado antes de producción:
- Servir todo por HTTPS (agregar un `Dockerfile`/certificado a nginx, o poner
  este stack completo detrás del OPNsense ya configurado en el proyecto,
  aprovechando las reglas de firewall y el aislamiento por VLAN ya
  documentado).
- Rotar `GUACAMOLE_ADMIN_PASS`, `ADMIN_API_KEY` y `GUACAMOLE_DB_PASS` por
  valores generados, no dejarlos con placeholders.
- Limitar el acceso a `/servicios/contratar` (por ejemplo con un captcha o
  autenticación de cuenta) para que no cualquiera pueda crear VMs sin control.
- Definir qué pasa con el `usuario_acceso`/`llave_acceso` si el cliente cancela
  el servicio — hoy no hay endpoint que revoque el acceso ni borre el usuario
  de Guacamole cuando se elimina la VM (se puede agregar a `eliminar_vm` una
  llamada para borrar también el usuario y la conexión correspondientes en
  Guacamole).
