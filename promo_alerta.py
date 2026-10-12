"""
Promo Alerta: busca promoções com 50%+ de desconto ou com "bug"/erro de preço
no Pelando e no Promobit e manda no WhatsApp via CallMeBot.

Uso:
  python promo_alerta.py            -> roda normal (busca, filtra, envia o que é novo)
  python promo_alerta.py --teste    -> só mostra na tela o que encontraria (não envia, não salva)
  python promo_alerta.py --ping     -> manda uma mensagem de teste no WhatsApp
  python promo_alerta.py --so-pelando -> lê só o Pelando (usado no PC; a nuvem lê o Promobit)
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent
CONFIG = PASTA / "config.json"
VISTOS = PASTA / "vistos.json"
LOG = PASTA / "log.txt"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

PALAVRAS_BUG = re.compile(
    r"\bbug(ad[oa]s?|s)?\b|erro de pre[cç]o|pre[cç]o errado|pre[cç]o bugado|glitch",
    re.IGNORECASE,
)


def log(msg):
    linha = f"[{datetime.now():%d/%m %H:%M:%S}] {msg}"
    print(linha)
    with open(LOG, "a", encoding="utf8") as f:
        f.write(linha + "\n")
    # mantém o log pequeno
    linhas = LOG.read_text(encoding="utf8").splitlines()
    if len(linhas) > 2000:
        LOG.write_text("\n".join(linhas[-1000:]) + "\n", encoding="utf8")


def baixar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf8", "replace")


# ---------- Pelando ----------

def _astro(v):
    """Decodifica o formato de props do Astro: cada valor vem como [tipo, valor]."""
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        tipo, val = v
        if tipo == 1:  # array
            return [_astro(x) for x in val]
        return _astro(val)
    if isinstance(v, dict):
        return {k: _astro(x) for k, x in v.items()}
    return v


def pelando():
    pagina = baixar("https://www.pelando.com.br/recentes")
    ofertas = []
    for props in re.findall(r'<astro-island[^>]*component-export="RecentsFeedContent"[^>]*props="([^"]*)"', pagina):
        dados = _astro(json.loads(html.unescape(props)))
        for d in dados.get("initialDeals") or []:
            if d.get("status") not in (None, "active"):
                continue
            ofertas.append({
                "id": "pelando:" + str(d["id"]),
                "titulo": d.get("title") or "",
                "preco": d.get("price"),
                "desconto": d.get("discountPercentage") or 0,
                "loja": (d.get("store") or {}).get("name") or "",
                "link": d.get("sourceUrl") or "https://www.pelando.com.br/d/" + d["slug"],
                "site": "Pelando",
            })
    return ofertas


# ---------- Promobit ----------

def promobit():
    dados = json.loads(baixar("https://api.promobit.com.br/offers/recents?sort=latest&limit=50"))
    ofertas = []
    for o in dados.get("offers") or []:
        preco = o.get("offer_price") or 0
        antigo = o.get("offer_old_price") or 0
        desc = o.get("offer_discont_percentage") or 0
        if not desc and antigo and preco and antigo > preco:
            desc = (1 - preco / antigo) * 100
        ofertas.append({
            "id": "promobit:" + str(o["offer_id"]),
            "titulo": o.get("offer_title") or "",
            "preco": preco or None,
            "desconto": round(desc),
            "loja": o.get("store_name") or "",
            "link": f"https://www.promobit.com.br/oferta/{o['offer_slug']}/",
            "promobit_id": o["offer_id"],
            "site": "Promobit",
        })
    return ofertas


def link_da_loja(o):
    """Troca o link do Promobit pelo link direto da loja (só para as ofertas que vão ser enviadas)."""
    if "promobit_id" not in o:
        return
    try:
        pagina = baixar(f"https://www.promobit.com.br/Redirect/to/{o['promobit_id']}/")
        for url in re.findall(r'(?:location(?:\.href)?\s*=\s*|url=|href=)["\']?(https?://[^"\'\s>]+)', pagina):
            if "promobit.com.br" not in url:
                o["link"] = html.unescape(url)
                return
    except Exception as e:
        log(f"Não achei o link da loja da oferta {o['promobit_id']}: {e}")


# ---------- Filtro e envio ----------

def interessa(o):
    if (o["desconto"] or 0) >= 50:
        return True
    return bool(PALAVRAS_BUG.search(o["titulo"]))


def formatar(o):
    tag = "🐞 BUG/ERRO DE PREÇO" if PALAVRAS_BUG.search(o["titulo"]) else f"🔥 {o['desconto']}% OFF"
    preco = f"R$ {o['preco']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if o["preco"] else ""
    partes = [f"*{tag}*", o["titulo"]]
    linha = " · ".join(x for x in [preco, o["loja"], o["site"]] if x)
    if linha:
        partes.append(linha)
    partes.append(o["link"])
    return "\n".join(partes)


def enviar(cfg, texto):
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode({
        "phone": cfg["telefone"],
        "text": texto,
        "apikey": cfg["apikey"],
    })
    resp = baixar(url)
    if "queued" not in resp.lower() and "sent" not in resp.lower():
        raise RuntimeError("CallMeBot respondeu: " + re.sub(r"<[^>]+>|\s+", " ", resp)[:200])


def carregar_config():
    # no servidor (GitHub Actions) os dados vêm dos "secrets"
    if os.environ.get("CALLMEBOT_APIKEY"):
        return {"telefone": os.environ["CALLMEBOT_TELEFONE"], "apikey": os.environ["CALLMEBOT_APIKEY"]}
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps({"telefone": "+55DDDNUMERO", "apikey": "COLE_A_APIKEY_AQUI"},
                                     indent=2), encoding="utf8")
    cfg = json.loads(CONFIG.read_text(encoding="utf8"))
    if "AQUI" in cfg["apikey"] or "DDD" in cfg["telefone"]:
        log("Preencha telefone e apikey no config.json antes de usar.")
        sys.exit(1)
    return cfg


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf8", errors="replace")
    teste = "--teste" in sys.argv

    if "--ping" in sys.argv:
        enviar(carregar_config(), "✅ Promo Alerta funcionando! Você vai receber aqui as promoções de 50%+ e os bugs de preço.")
        log("Mensagem de teste enviada.")
        return

    # o Pelando bloqueia servidores, então a nuvem lê só o Promobit
    # e o PC (com --so-pelando) lê só o Pelando
    fontes = [("Pelando", pelando), ("Promobit", promobit)]
    if os.environ.get("GITHUB_ACTIONS"):
        fontes = [("Promobit", promobit)]
    elif "--so-pelando" in sys.argv:
        fontes = [("Pelando", pelando)]

    ofertas = []
    for nome, fonte in fontes:
        try:
            achadas = fonte()
            ofertas += achadas
        except Exception as e:
            log(f"Erro ao ler {nome}: {e}")

    boas = [o for o in ofertas if interessa(o)]

    if teste:
        for o in boas:
            link_da_loja(o)
        print(f"{len(ofertas)} ofertas lidas, {len(boas)} passam no filtro:\n")
        for o in boas:
            print(formatar(o), "\n")
        return

    vistos = json.loads(VISTOS.read_text(encoding="utf8")) if VISTOS.exists() else None
    primeira_vez = vistos is None
    vistos = vistos or {}
    agora = time.time()
    novas = [o for o in boas if o["id"] not in vistos]

    if primeira_vez:
        # na primeira execução só marca o que já existe, para não lotar o WhatsApp
        for o in boas:
            vistos[o["id"]] = agora
        log(f"Primeira execução: {len(boas)} ofertas atuais marcadas como vistas.")
    elif novas:
        cfg = carregar_config()
        for o in novas:
            link_da_loja(o)
        # agrupa até 5 ofertas por mensagem
        for i in range(0, len(novas), 5):
            bloco = novas[i:i + 5]
            enviar(cfg, "\n\n".join(formatar(o) for o in bloco))
            for o in bloco:
                vistos[o["id"]] = agora
            time.sleep(5)
        log(f"{len(novas)} oferta(s) nova(s) enviada(s).")
    else:
        log(f"Nada novo ({len(ofertas)} ofertas lidas).")

    # esquece o que tem mais de 7 dias
    vistos = {k: t for k, t in vistos.items() if agora - t < 7 * 86400}
    VISTOS.write_text(json.dumps(vistos), encoding="utf8")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Erro: {e}")
        sys.exit(1)
