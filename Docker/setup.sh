#!/usr/bin/env bash
# Se corre UNA SOLA VEZ, antes del primer "docker compose up".
# Genera el esquema SQL que Guacamole necesita en su base de datos
# (tablas de usuarios, conexiones, permisos, historial de sesiones).
#
# Por qué existe este script: la imagen oficial de guacamole/guacamole trae
# el generador del esquema adentro, pero no lo ejecuta sola — hay que pedírselo
# una vez y guardar el resultado como archivo, que postgres luego carga solo
# al arrancar por primera vez (todo lo que esté en ./init se auto-ejecuta
# gracias a docker-entrypoint-initdb.d).

set -e

mkdir -p init

if [ -f init/01-initdb.sql ]; then
  echo "init/01-initdb.sql ya existe, no se vuelve a generar."
  echo "Si quieres regenerarlo (por ejemplo tras cambiar de versión de Guacamole), bórralo primero."
  exit 0
fi

echo "Generando el esquema de base de datos de Guacamole..."
docker run --rm guacamole/guacamole:1.5.5 /opt/guacamole/bin/initdb.sh --postgresql > init/01-initdb.sql

echo "Listo: init/01-initdb.sql generado."
echo "Ahora sí puedes correr: cp .env.example .env  (y llenarlo)  ->  docker compose up -d --build"
