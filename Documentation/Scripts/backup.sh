#!/bin/bash

# Aquí pon tu ruta local y la del servidor de copias
ruta_local="./"
remote_path="/home/carpeta_de_los_backups/"

# Aquí pon tus credenciales
ssh_user="user"
ssh_server="192.168.100.22"

# Esto solo es para mantener control del proceso
intento=1
max_intentos=3
exito=false

echo "Haciendo el backup con rsync..."

while [ $intento -le $max_intentos ] && [ "$exito" = false ]; do
  echo "Intento $intento de $max_intentos"

  # Aquí puedes usar más intentos, pero le puse 3 solo como ejemplo.

  # Los parámetros que usualmente se usan. Igual aquí te dejo el link:
  # https://www.hostgator.mx/blog/como-usar-rsync/
  #
  # -a: Es el modo archivo; mantiene permisos, propietario, fechas, etc.
  # -v: Muestra los archivos que se están enviando en ese momento.
  # -z: Comprime todo para que vaya más rápido.
  # --partial: Si se traba o se cae la red, guarda lo que se alcanzó a subir
  #            para no empezar otra vez.

  rsync -avz --partial "$ruta_local" "$ssh_user"@"$ssh_server":"$remote_path"

  if [[ $? -eq 0 ]]; then
    echo "Se enviaron todos los archivos."
    exito=true
  else
    echo "Hubo algún error en el intento $intento."

    if [ $intento -lt $max_intentos ]; then
      echo "Esperando 5 segundos para ver si el problema de red o el error se resuelve..."
      sleep 5
    fi

    ((intento++)) # Contador de intentos
  fi

  echo
done

if [ "$exito" = true ]; then
  echo "El backup se realizó correctamente."
  exit 0
else
  echo "Hubo demasiados errores durante el proceso."
  exit 1
fi
