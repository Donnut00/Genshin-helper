import os
import sys
import time
import socket
import threading
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler

PORT = 8765
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
USER_SCRIPT_PATH = os.path.join(PROJECT_ROOT, "tampermonkey", "hoyolab_sync.user.js")

class UserscriptHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress console spam

    def do_GET(self):
        if self.path.startswith("/hoyolab_sync.user.js") or self.path.startswith("/script.js") or self.path.endswith(".user.js"):
            try:
                with open(USER_SCRIPT_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_response(404)
            self.end_headers()

def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0

def copy_to_clipboard(text: str):
    try:
        # Use clip.exe or PowerShell
        p = subprocess.Popen(["clip.exe"], stdin=subprocess.PIPE, shell=True)
        p.communicate(input=text.encode("utf-8"))
    except Exception:
        pass

def find_browser_cmd():
    brave_paths = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
    ]
    for p in brave_paths:
        if os.path.exists(p):
            return p
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
    ]
    for p in chrome_paths:
        if os.path.exists(p):
            return p
    return None

def main():
    os.system("color 0b" if sys.platform == "win32" else "")
    print("\n" + "=" * 62)
    print("      GENSHIN HELPER - TAMPERMONKEY 1-CLICK UPDATER")
    print("=" * 62)

    if not os.path.exists(USER_SCRIPT_PATH):
        print(f"\n[!] Khong tim thay file: {USER_SCRIPT_PATH}")
        input("\nNhan Enter de thoat...")
        return

    with open(USER_SCRIPT_PATH, "r", encoding="utf-8") as f:
        script_code = f.read()

    # Extract version
    version = "Unknown"
    for line in script_code.splitlines()[:20]:
        if "@version" in line:
            version = line.split("@version")[-1].strip()
            break

    print(f"\n[1/3] Tim thay UserScript: hoyolab_sync.user.js (v{version})")

    # Step 2: Copy to clipboard
    copy_to_clipboard(script_code)
    print("[2/3] Da sao chep toan bo code vao Clipboard!")

    # Step 3: Check server
    started_temp_server = False
    httpd = None
    if not is_port_open(PORT):
        print(f"[*] Dang khoi chay may chu cap phat tam thoi tai cong {PORT}...")
        try:
            httpd = HTTPServer(("127.0.0.1", PORT), UserscriptHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            started_temp_server = True
            time.sleep(0.3)
        except Exception as e:
            print(f"[!] Khong the mo port {PORT}: {e}")

    # Step 4: Open Browser
    target_url = f"http://127.0.0.1:{PORT}/hoyolab_sync.user.js"
    browser = find_browser_cmd()

    print("[3/3] Dang mo tab cap nhat Tampermonkey tren trinh duyet...")
    if browser:
        subprocess.Popen([browser, target_url])
    else:
        import webbrowser
        webbrowser.open(target_url)

    print("\n" + "-" * 62)
    print("  HOAN TAT! Tren trinh duyet cua ban:")
    print("  1. Tampermonkey se tu dong mo tab cai dat UserScript.")
    print("  2. Nhan nut [ Cap nhat ] hoac [ Cai dat lai (Reinstall) ].")
    print("  3. F5 lai trang ban do HoYoLAB de bat dau su dung!")
    print("  (Code cung da duoc copy san vao Clipboard neu muon dan thu cong)")
    print("-" * 62)

    if started_temp_server:
        print("\nDang giu ket noi 10 giay de trinh duyet tai file...")
        for i in range(10, 0, -1):
            time.sleep(1)
            print(f"\rDong may chu tam thoi sau: {i}s  ", end="", flush=True)
        print("\nDa dong may chu tam thoi.")
    else:
        time.sleep(2)

    print("\nNhan Enter hoac dong cua so nay...")

if __name__ == "__main__":
    main()
