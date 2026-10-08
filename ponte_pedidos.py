"""
Ponte entre o site Burger Mestre e a API de despacho (api_despacho3.py).

Por que existe: o site é aberto por clientes, então a API_KEY não pode ficar no navegador.
O site fala só com esta ponte; ela guarda a chave, descobre lat/lng pelo /geocodificar
da sua API e cria o pedido em /pedidos.

Variáveis de ambiente (no Render):
    DESPACHO_URL   endereço da sua API de despacho, ex.: https://sua-api.onrender.com
    API_KEY        a mesma API_KEY da API de despacho
    SITE_ORIGENS   (opcional) endereços do site permitidos, separados por vírgula.
                   Não precisa se o site for servido por esta própria ponte (rota "/").
    DESPACHO_AUTOMATICO_SEG  (opcional) se > 0, chama /despachar sozinho X segundos depois
                   do primeiro pedido, dando tempo de agrupar. Padrão 0 = você despacha no painel.
Rodar:  uvicorn ponte_pedidos:app --host 0.0.0.0 --port 8000
"""
import json
import os
import threading
import time
import urllib.request
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

DESPACHO_URL = os.environ.get("DESPACHO_URL", "").strip().rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
ORIGENS = [o.strip() for o in os.environ.get("SITE_ORIGENS", "").split(",") if o.strip()]
AUTO_SEG = int(os.environ.get("DESPACHO_AUTOMATICO_SEG", "0") or 0)

app = FastAPI(title="Ponte Burger Mestre")
app.add_middleware(CORSMiddleware, allow_origins=ORIGENS, allow_methods=["POST"], allow_headers=["Content-Type"])


class PedidoSite(BaseModel):
    id: str = Field(pattern=r"^BM[A-Z0-9]{4,40}$")
    rua: str = Field(min_length=3, max_length=150)
    numero: str = Field(min_length=1, max_length=20)
    bairro: str = Field(min_length=2, max_length=80)
    complemento: Optional[str] = Field(default=None, max_length=120)
    referencia: Optional[str] = Field(default=None, max_length=200)
    cidade: str = Field(min_length=2, max_length=80)
    estado: str = Field(default="", max_length=40)


def _chamar(caminho, corpo):
    req = urllib.request.Request(
        DESPACHO_URL + caminho, data=json.dumps(corpo).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


# Limite simples por IP (10 pedidos a cada 10 min) para ninguém encher sua fila.
_acessos = defaultdict(list)


def _limitar(ip):
    agora = time.time()
    _acessos[ip] = [t for t in _acessos[ip] if agora - t < 600]
    if len(_acessos[ip]) >= 10:
        raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos.")
    _acessos[ip].append(agora)


_timer = None


def _agendar_despacho():
    global _timer
    if AUTO_SEG <= 0 or (_timer and _timer.is_alive()):
        return
    def rodar():
        try:
            _chamar("/despachar", {})
        except Exception:
            pass
    _timer = threading.Timer(AUTO_SEG, rodar)
    _timer.start()


@app.post("/pedido-site")
def receber(p: PedidoSite, request: Request):
    _limitar(request.client.host if request.client else "?")
    if not DESPACHO_URL or not API_KEY:
        raise HTTPException(500, "Ponte não configurada")
    estado = f", {p.estado}" if p.estado else ""
    num = "" if p.numero.upper() in ("S/N", "SN") else f", {p.numero}"
    try:
        g = _chamar("/geocodificar", {"endereco": f"{p.rua}{num}, {p.bairro}, {p.cidade}{estado}",
                                      "rua": p.rua, "numero": p.numero, "cidade": p.cidade, "estado": p.estado})
    except Exception:
        raise HTTPException(502, "Serviço de rotas indisponível. Tente novamente.")
    if g.get("aviso"):
        raise HTTPException(502, "Serviço de endereços indisponível. Tente novamente.")
    if not g.get("resultados"):
        raise HTTPException(422, "Não encontramos esse endereço. Confira rua, número e bairro.")
    ponto = g["resultados"][0]
    try:
        _chamar("/pedidos", {
            "id": p.id, "lat": ponto["lat"], "lng": ponto["lng"],
            "endereco": f"{p.rua}{num} - {p.bairro}, {p.cidade}" + (f" - {p.estado}" if p.estado else ""),
            "rua": p.rua, "numero": p.numero, "bairro": p.bairro,
            "complemento": p.complemento, "referencia": p.referencia, "cep": None})
    except Exception:
        raise HTTPException(502, "Não foi possível registrar a entrega. Tente novamente.")
    _agendar_despacho()
    return {"id": p.id, "lat": ponto["lat"], "lng": ponto["lng"]}


@app.get("/")
def site():
    """Serve o site (burger-mestre.html na mesma pasta), assim não há problema de CORS."""
    if os.path.exists("burger-mestre.html"):
        return FileResponse("burger-mestre.html")
    raise HTTPException(404, "Coloque burger-mestre.html na mesma pasta desta ponte.")
