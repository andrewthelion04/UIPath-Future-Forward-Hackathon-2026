"""
Script de verificare pentru un API key OpenRouter.

Utilizare:
    # Varianta 1: key-ul in variabila de mediu (recomandat)
    #   PowerShell:  $env:OPENROUTER_API_KEY = "sk-or-..."
    python keyTestScript.py

    # Varianta 2: alt model / alta intrebare
    python keyTestScript.py --model "anthropic/claude-sonnet-4" --question "Cat face 2+2?"

Daca variabila de mediu nu e setata, scriptul iti cere key-ul (input ascuns).
Nu foloseste librarii externe (doar biblioteca standard Python).
"""

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_QUESTION = "Raspunde scurt: care este capitala Romaniei?"


def http_request(method, url, api_key, payload=None, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Title", "OpenRouter Key Test")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        return e.code, body


def check_key_info(api_key):
    """Interogheaza endpoint-ul /key pentru a valida key-ul si a vedea limitele."""
    print("[1/2] Verific key-ul (GET /key)...")
    status, body = http_request("GET", f"{BASE_URL}/key", api_key)
    if status != 200:
        print(f"  EROARE {status}: {body}")
        return False
    info = body.get("data", {})
    print("  OK - key valid")
    print(f"  Label:        {info.get('label')}")
    print(f"  Folosit:      {info.get('usage')} credite")
    print(f"  Limita:       {info.get('limit')}  (None = nelimitat)")
    print(f"  Free tier:    {info.get('is_free_tier')}")
    return True


def ask_question(api_key, model, question):
    """Trimite o intrebare de test la modelul ales (POST /chat/completions)."""
    print(f"\n[2/2] Trimit intrebarea la modelul '{model}'...")
    print(f"  Intrebare: {question}")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "max_tokens": 200,
    }
    start = time.time()
    status, body = http_request("POST", f"{BASE_URL}/chat/completions", api_key, payload)
    elapsed = time.time() - start

    if status != 200 or "choices" not in body:
        print(f"  EROARE {status}: {body}")
        return False

    answer = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    print(f"  Raspuns ({elapsed:.2f}s): {answer.strip()}")
    print(f"  Model folosit: {body.get('model')}")
    print(
        f"  Tokeni: prompt={usage.get('prompt_tokens')}, "
        f"completion={usage.get('completion_tokens')}, total={usage.get('total_tokens')}"
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="Testeaza un API key OpenRouter.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model (implicit: {DEFAULT_MODEL})")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Intrebarea de test")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY") or getpass.getpass("OpenRouter API key: ")
    api_key = api_key.strip()
    if not api_key:
        print("Nu a fost furnizat niciun API key.")
        sys.exit(1)

    key_ok = check_key_info(api_key)
    chat_ok = ask_question(api_key, args.model, args.question) if key_ok else False

    print("\nREZULTAT:", "SUCCES - key-ul functioneaza!" if chat_ok else "ESEC - vezi erorile de mai sus.")
    sys.exit(0 if chat_ok else 1)


if __name__ == "__main__":
    main()
