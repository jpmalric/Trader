#!/usr/bin/env python3
"""Connexion 2FA unique à Trade Republic — crée/sauvegarde la session (.tr_cookies.txt).

Flux (un seul process, pour garder l'état du 2FA en mémoire) :
  1. tente de reprendre une session existante
  2. sinon déclenche l'envoi du code 2FA  -> écrit "code_sent:<s>" dans .tr_login_status
  3. attend que .tr_code.txt apparaisse (rempli par Claude quand tu donnes le code)
  4. valide le code et sauvegarde la session -> "logged_in"

Identifiants lus dans les variables d'env TR_PHONE / TR_PIN.
"""
import os
import time
from pathlib import Path

from pytr.api import TradeRepublicApi

REPO = Path(__file__).resolve().parent
STATUS = REPO / ".tr_login_status"
CODE = REPO / ".tr_code.txt"
COOKIES = REPO / ".tr_cookies.txt"


def w(s: str):
    STATUS.write_text(s, encoding="utf-8")


def main():
    phone = os.environ.get("TR_PHONE", "")
    pin = os.environ.get("TR_PIN", "")
    if not phone or not pin:
        w("error:no_credentials")
        print("ERROR no TR_PHONE/TR_PIN", flush=True)
        return 1

    tr = TradeRepublicApi(
        phone_no=phone, pin=pin, locale="fr",
        save_cookies=True, cookies_file=str(COOKIES),
        waf_token=os.environ.get("TR_WAF", "awswaf"),
    )

    # 1) session existante ?
    try:
        if tr.resume_websession():
            w("logged_in:resumed")
            print("LOGGED_IN resumed", flush=True)
            return 0
    except Exception:
        pass

    # 2) déclenche le 2FA
    try:
        countdown = tr.initiate_weblogin()
    except Exception as e:
        w(f"error:init:{e!r}")
        print("ERROR_INIT", repr(e), flush=True)
        return 1
    w(f"code_sent:{countdown}")
    print("CODE_SENT", countdown, flush=True)

    # 3) attend le code (fourni via .tr_code.txt)
    if CODE.exists():
        CODE.unlink()
    deadline = time.time() + 180
    code = None
    while time.time() < deadline:
        if CODE.exists():
            code = CODE.read_text(encoding="utf-8").strip()
            if code:
                break
        time.sleep(1)
    if not code:
        w("error:timeout")
        print("TIMEOUT waiting for code", flush=True)
        return 1

    # 4) valide
    try:
        tr.complete_weblogin(code)
    except Exception as e:
        w(f"error:complete:{e!r}")
        print("ERROR_COMPLETE", repr(e), flush=True)
        return 1
    finally:
        try:
            CODE.unlink()
        except Exception:
            pass

    w("logged_in")
    print("LOGGED_IN", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
