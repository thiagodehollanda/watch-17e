#!/usr/bin/env python3
"""
Monitor diario de preco — envia o menor preco do dia no Telegram.

Fontes:
  1. API do Mercado Livre  -> preco de balcao (piso de referencia)
  2. Feeds RSS de agregadores de oferta -> precos com cupom (o que realmente importa)

Variaveis de ambiente:
  TELEGRAM_BOT_TOKEN  (obrigatorio)
  TELEGRAM_CHAT_ID    (obrigatorio)
  ML_ACCESS_TOKEN     (opcional; sem ele a fonte Mercado Livre e pulada)
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

BRT = timezone(timedelta(hours=-3))
RAIZ = Path(__file__).resolve().parent
CONFIG = json.loads((RAIZ / "config.json").read_text(encoding="utf-8"))
ESTADO = RAIZ / "state.json"
HISTORICO = RAIZ / "historico.csv"

UA = "Mozilla/5.0 (compatible; monitor-preco-pessoal/1.0)"
TIMEOUT = 25

RE_PRECO = re.compile(r"R\$\s*([\d]{1,3}(?:\.\d{3})*,\d{2})")
# "cupom" pode vir em qualquer caixa, mas o codigo do cupom e sempre CAIXA ALTA.
# Nao use re.IGNORECASE aqui: senao "com cupom iPhone..." casa com "IPHONE".
RE_CUPOM = re.compile(
    r"(?i:cupo(?:m|ns))\s*"          # "cupom" ou "cupons"
    r"(?:(?i:de\s+desconto)\s*)?"    # "de desconto" opcional
    r"[:\-]?\s*"
    r"(?:(?i:o|do|no|:)\s+)?"        # "com o cupom X", "cupom do X"
    r"([A-Z][A-Z0-9]{3,15})\b"
)
RE_PIX = re.compile(r"\bpix\b", re.IGNORECASE)

# tokens em caixa alta que aparecem perto de "cupom" mas nao sao cupom
CUPOM_FALSO = {
    "PIX", "OFF", "AMAZON", "MAGALU", "SHOPEE", "APPLE", "IPHONE", "SAMSUNG",
    "GALAXY", "BLACK", "FRIDAY", "PRIME", "DAY", "NOVO", "MENOR", "PRECO",
    "CUPOM", "DESCONTO", "OFERTA", "COMPRE", "AGORA", "CLIQUE", "AQUI",
}


# --------------------------------------------------------------------------- #
# util
# --------------------------------------------------------------------------- #
def norm(txt: str) -> str:
    """minusculas, sem acento — para casar termos de filtro."""
    txt = unicodedata.normalize("NFKD", txt or "")
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    return txt.lower()


def brl_para_float(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def float_para_brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def agora() -> datetime:
    return datetime.now(BRT)


def log(msg: str) -> None:
    print(f"[{agora():%d/%m/%Y %H:%M}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# modelo
# --------------------------------------------------------------------------- #
@dataclass
class Oferta:
    preco: float
    loja: str
    fonte: str
    titulo: str
    url: str
    cupom: str | None = None
    pix: bool = False

    @property
    def rotulo(self) -> str:
        partes = [self.loja]
        if self.cupom:
            partes.append(f"cupom {self.cupom}")
        if self.pix:
            partes.append("Pix")
        return " · ".join(partes)


# --------------------------------------------------------------------------- #
# filtro
# --------------------------------------------------------------------------- #
def relevante(titulo: str) -> bool:
    t = norm(titulo)
    if not all(norm(x) in t for x in CONFIG["termos_obrigatorios"]):
        return False
    if any(norm(x) in t for x in CONFIG["termos_proibidos"]):
        return False
    return True


def preco_plausivel(v: float) -> bool:
    return CONFIG["piso_sanidade"] <= v <= CONFIG["teto_sanidade"]


# --------------------------------------------------------------------------- #
# fonte 1 — Mercado Livre
# --------------------------------------------------------------------------- #
def coletar_mercado_livre() -> list[Oferta]:
    token = os.getenv("ML_ACCESS_TOKEN", "").strip()
    if not token:
        log("ML: sem ML_ACCESS_TOKEN, fonte pulada.")
        return []

    url = "https://api.mercadolibre.com/sites/MLB/search"
    params = {
        "q": CONFIG["ml_query"],
        "limit": CONFIG["ml_max_resultados"],
        "sort": "price_asc",
        "condition": "new",
    }
    try:
        r = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "User-Agent": UA},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        dados = r.json()
    except Exception as e:  # noqa: BLE001
        log(f"ML: falha na consulta ({e}). Fonte ignorada.")
        return []

    ofertas: list[Oferta] = []
    esperado = CONFIG["armazenamento_esperado"]
    for item in dados.get("results", []):
        titulo = item.get("title", "")
        if not relevante(titulo):
            continue
        if esperado and esperado not in norm(titulo).replace(" ", ""):
            continue
        preco = float(item.get("price") or 0)
        if not preco_plausivel(preco):
            continue
        ofertas.append(
            Oferta(
                preco=preco,
                loja=(item.get("seller", {}) or {}).get("nickname") or "Mercado Livre",
                fonte="Mercado Livre (API)",
                titulo=titulo,
                url=item.get("permalink", ""),
            )
        )
    log(f"ML: {len(ofertas)} oferta(s) validas.")
    return ofertas


# --------------------------------------------------------------------------- #
# fonte 2 — feeds de agregadores
# --------------------------------------------------------------------------- #
def _texto_entrada(entrada) -> str:
    partes = [entrada.get("title", ""), entrada.get("summary", "")]
    for c in entrada.get("content", []) or []:
        partes.append(c.get("value", ""))
    return html.unescape(re.sub(r"<[^>]+>", " ", " ".join(partes)))


def _extrair_cupom(texto: str) -> str | None:
    for m in RE_CUPOM.finditer(texto):
        cand = m.group(1)
        if cand in CUPOM_FALSO or cand.isdigit():
            continue
        return cand
    return None


def _texto_artigo(url: str) -> str:
    """Baixa a materia. O resumo do RSS costuma cortar o codigo do cupom."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log(f"Artigo {url}: nao foi possivel abrir ({e}).")
        return ""
    corpo = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", r.text)
    return html.unescape(re.sub(r"<[^>]+>", " ", corpo))


def coletar_feeds() -> list[Oferta]:
    ofertas: list[Oferta] = []
    limite = agora() - timedelta(days=51)

    for url_feed in CONFIG["feeds"]:
        try:
            r = requests.get(url_feed, headers={"User-Agent": UA}, timeout=TIMEOUT)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            log(f"Feed {url_feed}: indisponivel ({e}).")
            continue

        origem = feed.feed.get("title", url_feed)
        achados = 0

        for entrada in feed.entries:
            titulo = entrada.get("title", "")
            if not relevante(titulo):
                continue

            pub = entrada.get("published_parsed") or entrada.get("updated_parsed")
            if pub:
                dt = datetime(*pub[:6], tzinfo=timezone.utc).astimezone(BRT)
                if dt < limite:
                    continue

            texto = _texto_entrada(entrada)
            link = entrada.get("link", "")
            cupom = _extrair_cupom(texto)
            precos = [brl_para_float(p) for p in RE_PRECO.findall(texto)]
            precos = [p for p in precos if preco_plausivel(p)]

            # o resumo do RSS quase sempre corta o cupom e as vezes o preco:
            # nesse caso vale abrir a materia.
            if link and (cupom is None or not precos):
                corpo = _texto_artigo(link)
                if corpo:
                    cupom = cupom or _extrair_cupom(corpo)
                    extras = [brl_para_float(p) for p in RE_PRECO.findall(corpo)]
                    precos += [p for p in extras if preco_plausivel(p)]

            if not precos:
                continue

            ofertas.append(
                Oferta(
                    preco=min(precos),
                    loja=origem,
                    fonte=f"RSS · {origem}",
                    titulo=titulo,
                    url=entrada.get("link", ""),
                    cupom=cupom,
                    pix=bool(RE_PIX.search(texto)),
                )
            )
            achados += 1

        log(f"Feed {origem}: {achados} oferta(s).")

    return ofertas


# --------------------------------------------------------------------------- #
# estado e historico
# --------------------------------------------------------------------------- #
def ler_estado() -> dict:
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"menor_historico": None, "ultimo_reportado": None, "data_minimo": None}


def gravar_estado(estado: dict) -> None:
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def gravar_historico(oferta: Oferta | None) -> None:
    novo = not HISTORICO.exists()
    with HISTORICO.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["data", "preco", "loja", "cupom", "fonte", "url"])
        if oferta:
            w.writerow(
                [
                    f"{agora():%Y-%m-%d}",
                    f"{oferta.preco:.2f}",
                    oferta.loja,
                    oferta.cupom or "",
                    oferta.fonte,
                    oferta.url,
                ]
            )
        else:
            w.writerow([f"{agora():%Y-%m-%d}", "", "", "", "sem coleta", ""])


# --------------------------------------------------------------------------- #
# mensagem
# --------------------------------------------------------------------------- #
def montar_mensagem(ofertas: list[Oferta], estado: dict) -> str | None:
    data = f"{agora():%d/%m/%Y}"
    produto = CONFIG["produto"]

    if not ofertas:
        if CONFIG["silenciar_se_sem_novidade"]:
            return None
        return (
            f"<b>{html.escape(produto)}</b> — {data}\n\n"
            "Nenhuma oferta coletada hoje. Todas as fontes vieram vazias "
            "ou fora dos filtros."
        )

    ofertas = sorted(ofertas, key=lambda o: o.preco)
    melhor = ofertas[0]
    anterior = estado.get("ultimo_reportado")
    minimo = estado.get("menor_historico")

    if (
        CONFIG["silenciar_se_sem_novidade"]
        and anterior is not None
        and melhor.preco >= anterior
    ):
        return None

    linhas = [f"<b>{html.escape(produto)}</b> — {data}", ""]
    linhas.append(f"💰 <b>Menor do dia: {float_para_brl(melhor.preco)}</b>")
    linhas.append(f"{html.escape(melhor.rotulo)}")
    if melhor.url:
        linhas.append(f'<a href="{html.escape(melhor.url)}">abrir oferta</a>')
    linhas.append("")

    if anterior is not None:
        delta = melhor.preco - anterior
        if abs(delta) < 0.01:
            linhas.append("↔️ Estavel em relacao a ontem.")
        elif delta < 0:
            linhas.append(f"🔻 Caiu {float_para_brl(abs(delta))} desde ontem.")
        else:
            linhas.append(f"🔺 Subiu {float_para_brl(delta)} desde ontem.")

    if minimo is not None:
        if melhor.preco < minimo:
            linhas.append("🏆 <b>Menor preco ja registrado pelo monitor.</b>")
        else:
            linhas.append(
                f"Minimo historico: {float_para_brl(minimo)} "
                f"({estado.get('data_minimo', 's/ data')})"
            )

    if melhor.preco <= CONFIG["preco_alvo"]:
        linhas.append(f"✅ <b>Abaixo do alvo de {float_para_brl(CONFIG['preco_alvo'])}.</b>")
    else:
        falta = melhor.preco - CONFIG["preco_alvo"]
        linhas.append(f"⏳ Falta {float_para_brl(falta)} para o alvo.")

    outras = ofertas[1:4]
    if outras:
        linhas.append("")
        linhas.append("<b>Outras:</b>")
        for o in outras:
            link = f' — <a href="{html.escape(o.url)}">link</a>' if o.url else ""
            linhas.append(f"• {float_para_brl(o.preco)} — {html.escape(o.rotulo)}{link}")

    linhas.append("")
    linhas.append("<i>Cupons vencem rapido. Confira o valor no carrinho.</i>")
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #
def enviar_telegram(texto: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("ERRO: TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID ausente.")
        sys.exit(1)

    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": texto,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=TIMEOUT,
    )
    if not r.ok:
        log(f"Telegram falhou: {r.status_code} {r.text[:300]}")
        r.raise_for_status()
    log("Telegram: mensagem enviada.")


# --------------------------------------------------------------------------- #
def main() -> None:
    ofertas = coletar_mercado_livre() + coletar_feeds()

    # deduplica: a mesma materia costuma aparecer em varios feeds
    vistos: set[str] = set()
    unicas: list[Oferta] = []
    for o in sorted(ofertas, key=lambda x: x.preco):
        chave = o.url.split("?")[0] or f"{o.preco:.2f}|{norm(o.loja)}"
        if chave not in vistos:
            vistos.add(chave)
            unicas.append(o)

    estado = ler_estado()
    texto = montar_mensagem(unicas, estado)

    melhor = unicas[0] if unicas else None
    gravar_historico(melhor)

    if melhor:
        minimo = estado.get("menor_historico")
        if minimo is None or melhor.preco < minimo:
            estado["menor_historico"] = round(melhor.preco, 2)
            estado["data_minimo"] = f"{agora():%d/%m/%Y}"
        estado["ultimo_reportado"] = round(melhor.preco, 2)
        estado["ultima_execucao"] = f"{agora():%d/%m/%Y %H:%M}"
        estado["melhor_oferta"] = asdict(melhor)
    gravar_estado(estado)

    if texto:
        enviar_telegram(texto)
    else:
        log("Nada novo. Mensagem suprimida por configuracao.")


if __name__ == "__main__":
    main()

