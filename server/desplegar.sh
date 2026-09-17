#!/usr/bin/env bash
# Despliegue de la API de deteccion de placas en una EC2 Ubuntu 24.04 limpia.
#
# Se ejecuta DENTRO del servidor. Desde tu PC:
#   scp -i llave.pem server/app.py server/desplegar.sh server/yolo-plates.service modelo/best.pt ubuntu@IP:/home/ubuntu/
#   ssh -i llave.pem ubuntu@IP "bash /home/ubuntu/desplegar.sh"
#
# Es idempotente: se puede volver a correr para actualizar el codigo.
set -euo pipefail

PROYECTO=/home/ubuntu/proyecto
ORIGEN=${ORIGEN:-/home/ubuntu}

echo "===== Despliegue iniciado $(date -Is) ====="

# --- 1. Swap -----------------------------------------------------------------
# La t3.micro tiene 1 GB de RAM. Instalar torch y cargar YOLO + EasyOCR a la vez
# la desborda: sin swap el proceso muere con OOM a mitad del arranque.
if [ ! -f /swapfile ]; then
  echo "--- creando swap de 4 GB ---"
  sudo fallocate -l 4G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# --- 2. Dependencias del sistema ---------------------------------------------
echo "--- paquetes del sistema ---"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-venv python3-pip libgl1 libglib2.0-0

# --- 3. Entorno virtual -------------------------------------------------------
mkdir -p "$PROYECTO"
cd "$PROYECTO"
[ -d venv ] || python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip -q

echo "--- torch CPU (sin CUDA) ---"
pip install --no-cache-dir -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
echo "--- ultralytics / easyocr / fastapi ---"
pip install --no-cache-dir -q \
  ultralytics easyocr fastapi "uvicorn[standard]" python-multipart \
  opencv-python-headless pillow numpy

# --- 4. Codigo y modelo -------------------------------------------------------
cp "$ORIGEN/app.py" "$PROYECTO/app.py"
[ -f "$ORIGEN/best.pt" ] && cp "$ORIGEN/best.pt" "$PROYECTO/best.pt"
[ -f "$PROYECTO/best.pt" ] || { echo "FALTA best.pt en $PROYECTO"; exit 1; }
mkdir -p "$PROYECTO/.ultralytics"

# --- 4b. Segundo motor de OCR: PaddleOCR, en su propio venv ---------------
# Va aparte a proposito: paddleocr exige numpy 2.3 y otro OpenCV, que romperian
# el venv del servidor (numpy 2.5 + torch + easyocr). Es OPCIONAL -- si falla la
# instalacion o se apaga el servicio, el servidor sigue con EasyOCR solo.
# Aporta: 10 lecturas correctas de 13 en el banco, frente a 9 sin el.
if [ "${CON_PADDLE:-1}" = "1" ]; then
  echo "--- PaddleOCR (motor secundario) ---"
  [ -d /home/ubuntu/exp-venv ] || python3 -m venv /home/ubuntu/exp-venv
  /home/ubuntu/exp-venv/bin/pip install --upgrade pip -q
  /home/ubuntu/exp-venv/bin/pip install --no-cache-dir -q paddlepaddle paddleocr fastapi "uvicorn[standard]" \
    || echo "PaddleOCR no se pudo instalar; se sigue sin el"
  cp "$ORIGEN/ocr_paddle.py" "$PROYECTO/ocr_paddle.py" 2>/dev/null || true
  if [ -f "$ORIGEN/ocr-paddle.service" ] && [ -f "$PROYECTO/ocr_paddle.py" ]; then
    sudo cp "$ORIGEN/ocr-paddle.service" /etc/systemd/system/ocr-paddle.service
    sudo systemctl daemon-reload
    sudo systemctl enable ocr-paddle.service
    sudo systemctl restart ocr-paddle.service
  fi
fi

# --- 5. Servicio systemd ------------------------------------------------------
# Libera el 8080 si lo tiene el servidor estatico del laboratorio anterior:
# la demo no se pierde, FastAPI la vuelve a servir en /web/.
if systemctl list-unit-files | grep -q '^bike.service'; then
  echo "--- liberando el 8080 (bike.service) ---"
  sudo systemctl stop bike.service || true
  sudo systemctl disable bike.service || true
fi

sudo cp "$ORIGEN/yolo-plates.service" /etc/systemd/system/yolo-plates.service
sudo systemctl daemon-reload
sudo systemctl enable yolo-plates.service
sudo systemctl restart yolo-plates.service

# --- 6. Verificacion ----------------------------------------------------------
# Cargar YOLO + EasyOCR tarda ~30 s en esta instancia.
echo "--- esperando a que la API responda ---"
for _ in $(seq 1 40); do
  if [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:8080/health || true)" = "200" ]; then
    echo "API arriba:"
    curl -s http://localhost:8080/health; echo
    echo "===== Despliegue OK $(date -Is) ====="
    exit 0
  fi
  sleep 5
done

echo "La API no respondio a tiempo. Revisa: sudo journalctl -u yolo-plates -n 50 --no-pager"
exit 1
