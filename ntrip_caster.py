import socket
import threading
import time
import base64
import json
import os
from queue import Queue, Empty
from datetime import datetime

CONFIG_FILE = "caster_config.json"

# ==============================================================================
# Lớp Worker: Kết nối đến Base nguồn và lấy dữ liệu RTCM
# (Không thay đổi so với phiên bản trước)
# ==============================================================================
class NtripClientWorker(threading.Thread):
    def __init__(self, config, data_queue):
        super().__init__()
        self.config = config
        self.data_queue = data_queue
        self.stop_event = threading.Event()
        self.name = f"ClientWorker-{config.get('mountpoint', 'UNKNOWN')}"
        self.daemon = True

    def _generate_gga(self):
        lat = self.config['location']['lat']
        lon = self.config['location']['lon']
        now = datetime.utcnow()
        time_str = now.strftime("%H%M%S.00")
        
        lat_abs = abs(lat)
        lat_deg = int(lat_abs)
        lat_min = (lat_abs - lat_deg) * 60
        lat_dir = "N" if lat >= 0 else "S"
        lat_nmea = f"{lat_deg:02d}{lat_min:06.3f}"
        
        lon_abs = abs(lon)
        lon_deg = int(lon_abs)
        lon_min = (lon_abs - lon_deg) * 60
        lon_dir = "E" if lon >= 0 else "W"
        lon_nmea = f"{lon_deg:03d}{lon_min:06.3f}"

        gga_body = f"$GPGGA,{time_str},{lat_nmea},{lat_dir},{lon_nmea},{lon_dir},1,12,1.0,10.0,M,0.0,M,,"
        checksum = 0
        for char in gga_body[1:]:
            checksum ^= ord(char)
        return f"{gga_body}*{checksum:02X}\r\n".encode()

    def run(self):
        print(f"[*] Bắt đầu {self.name}: Kết nối đến {self.config['host']}:{self.config['port']}/{self.config['mountpoint']}")
        while not self.stop_event.is_set():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(10)
                s.connect((self.config['host'], self.config['port']))
                
                credentials = f"{self.config.get('username', '')}:{self.config.get('password', '')}"
                auth_str = base64.b64encode(credentials.encode()).decode()
                
                request = (
                    f"GET /{self.config['mountpoint']} HTTP/1.1\r\n"
                    f"Host: {self.config['host']}\r\n"
                    f"Ntrip-Version: Ntrip/2.0\r\n"
                    f"User-Agent: PythonNTRIPCaster/1.0\r\n"
                    f"Authorization: Basic {auth_str}\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                )
                s.sendall(request.encode())
                
                response = s.recv(2048)
                if not (b"ICY 200 OK" in response or b"HTTP/1.1 200 OK" in response):
                    print(f"[!] {self.name}: Kết nối Base thất bại. Phản hồi: {response.decode(errors='ignore')}")
                    s.close()
                    time.sleep(10)
                    continue

                print(f"[+] {self.name}: Kết nối Base thành công. Bắt đầu nhận dữ liệu.")
                # Một số Caster yêu cầu GGA sau khi phản hồi OK
                if self.config.get('gga_interval', 0) > 0:
                    s.sendall(self._generate_gga())
                    last_gga_time = time.time()
                
                s.settimeout(15)
                
                while not self.stop_event.is_set():
                    data = s.recv(4096)
                    if not data:
                        print(f"[!] {self.name}: Mất kết nối đến Base. Sẽ kết nối lại...")
                        break
                    
                    self.data_queue.put(data)
                    
                    if self.config.get('gga_interval', 0) > 0 and (time.time() - last_gga_time >= self.config['gga_interval']):
                        s.sendall(self._generate_gga())
                        last_gga_time = time.time()
                        
            except (socket.error, socket.timeout) as e:
                print(f"[!] {self.name}: Lỗi socket ({e}). Đang thử kết nối lại sau 5 giây...")
            except Exception as e:
                print(f"[!] {self.name}: Lỗi không xác định ({e}). Đang thử kết nối lại sau 10 giây...")
            finally:
                if 's' in locals() and s:
                    s.close()
                if not self.stop_event.is_set():
                    time.sleep(5)
        print(f"[-] {self.name} đã dừng.")

    def stop(self):
        self.stop_event.set()

# ==============================================================================
# Lớp Handler: Xử lý từng kết nối từ Rover (***ĐÃ CẬP NHẬT***)
# ==============================================================================
class RoverHandler(threading.Thread):
    def __init__(self, client_socket, address, caster_config, rover_accounts, data_queue):
        super().__init__()
        self.client_socket = client_socket
        self.address = address
        self.caster_config = caster_config
        self.rover_accounts = rover_accounts
        self.data_queue = data_queue
        self.stop_event = threading.Event()
        self.name = f"RoverHandler-{address[0]}:{address[1]}"
        self.daemon = True

    def _is_authenticated(self, auth_header, mountpoint):
        """Kiểm tra mountpoint và xác thực người dùng. Trả về (bool, str)"""
        # 1. Kiểm tra Mountpoint
        if mountpoint != f"/{self.caster_config['mountpoint']}":
            return False, "Bad Mountpoint"
        
        # 2. Kiểm tra header xác thực
        if not auth_header:
            return False, "Authorization header is missing"

        try:
            # Tách header, ví dụ: "Authorization: Basic cm92ZXIxOnBhc3N3b3JkMTIz"
            auth_type, auth_token = auth_header.replace("Authorization: ", "").split()
            if auth_type.lower() != 'basic':
                return False, f"Unsupported Auth type: {auth_type}"
            
            # Giải mã base64
            decoded_creds = base64.b64decode(auth_token).decode()
            username, password = decoded_creds.split(':', 1)

            # Kiểm tra với danh sách tài khoản
            for acc in self.rover_accounts:
                if acc['username'] == username and acc['password'] == password:
                    return True, f"Authenticated as {username}"
            
            # Nếu không tìm thấy user/pass phù hợp
            return False, f"Invalid credentials for user '{username}'"

        except (ValueError, IndexError) as e:
            # Lỗi nếu header bị dị dạng (không có space, không có dấu ':')
            print(f"[!] Lỗi phân tích Auth Header: {e}")
            return False, "Malformed Authorization header"
        except Exception as e:
            # Các lỗi không mong muốn khác
            print(f"[!] Lỗi xác thực không mong muốn: {e}")
            return False, "Bad Auth Header (unexpected error)"

    def run(self):
        print(f"[+] Rover mới kết nối từ: {self.address}")
        try:
            self.client_socket.settimeout(10)
            request_data = self.client_socket.recv(2048).decode(errors='ignore')
            
            # === DEBUG: In ra yêu cầu gốc từ client ===
            print(f"--- [DEBUG] Raw request from {self.address} ---")
            print(request_data)
            print("------------------------------------------")
            
            if not request_data:
                print(f"[-] Không nhận được dữ liệu từ {self.address}. Đóng kết nối.")
                return

            headers = request_data.split('\r\n')
            request_line = headers[0]
            method, mountpoint, _ = request_line.split()

            # Tìm header Authorization (không phân biệt chữ hoa, chữ thường)
            auth_header = next((h for h in headers if h.lower().startswith('authorization:')), None)

            is_auth, reason = self._is_authenticated(auth_header, mountpoint)

            if not is_auth:
                print(f"[-] Rover {self.address} xác thực thất bại: {reason}")
                # Gửi phản hồi lỗi phù hợp
                if reason == "Bad Mountpoint":
                    self.client_socket.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
                else:
                    self.client_socket.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
                return

            print(f"[+] Rover {self.address} xác thực thành công: {reason}. Bắt đầu truyền dữ liệu.")
            self.client_socket.sendall(b"ICY 200 OK\r\n\r\n")
            
            self.client_socket.settimeout(None)
            
            # Lấy dữ liệu từ queue và gửi cho Rover
            while not self.stop_event.is_set():
                try:
                    rtcm_data = self.data_queue.get(timeout=15)
                    self.client_socket.sendall(rtcm_data)
                except Empty:
                    # Không có dữ liệu mới từ Base trong 15s
                    # Có thể là kết nối Base đã chết, vòng lặp sẽ tiếp tục chờ
                    continue
                except socket.error:
                    print(f"[-] Rover {self.address} đã ngắt kết nối.")
                    break
        except (socket.timeout, IndexError, ValueError):
            print(f"[-] Yêu cầu từ {self.address} không hợp lệ hoặc bị timeout khi chờ yêu cầu.")
        except Exception as e:
            print(f"[!] Lỗi không xác định trong {self.name}: {e}")
        finally:
            self.client_socket.close()
            print(f"[-] Đã đóng kết nối với Rover {self.address}.")


# ==============================================================================
# Lớp Caster Server chính và Hàm main
# (Không thay đổi so với phiên bản trước)
# ==============================================================================
class NtripCasterServer:
    def __init__(self, config):
        self.config = config
        self.caster_settings = config['caster_settings']
        self.base_connection_config = config['base_connection']
        self.rover_accounts = config['rover_accounts']
        self.rtcm_data_queue = Queue(maxsize=100)
        self.client_worker = None
        self.server_socket = None
        self.rover_handlers = []
        self.stop_event = threading.Event()

    def _handle_sourcetable_request(self, client_socket):
        print(f"[*] Gửi Sourcetable cho {client_socket.getpeername()}")
        sourcetable = self.caster_settings.get("sourcetable", "")
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain\r\n"
            f"Content-Length: {len(sourcetable)}\r\n"
            "Connection: close\r\n\r\n"
            f"{sourcetable}\r\n"
            "ENDSOURCETABLE\r\n"
        )
        client_socket.sendall(response.encode())
        client_socket.close()

    def start(self):
        print("=============================================")
        print("=== BẮT ĐẦU CHẠY NTRIP CASTER TRUNG GIAN ===")
        print("=============================================")

        self.client_worker = NtripClientWorker(self.base_connection_config, self.rtcm_data_queue)
        self.client_worker.start()

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            host = self.caster_settings['host']
            port = self.caster_settings['port']
            self.server_socket.bind((host, port))
            self.server_socket.listen(5)
            print(f"[+] Caster đang lắng nghe trên {host}:{port}")
        except OSError as e:
            print(f"[!] LỖI NGHIÊM TRỌNG: Không thể bind tới {host}:{port}. Lỗi: {e}")
            self.stop()
            return

        while not self.stop_event.is_set():
            try:
                self.server_socket.settimeout(1.0)
                client_socket, address = self.server_socket.accept()
                
                first_bytes = client_socket.recv(1024, socket.MSG_PEEK)
                if first_bytes.startswith(b'GET / '):
                    self._handle_sourcetable_request(client_socket)
                    continue

                handler = RoverHandler(client_socket, address, self.caster_settings, self.rover_accounts, self.rtcm_data_queue)
                handler.start()
                self.rover_handlers.append(handler)
                self.rover_handlers = [h for h in self.rover_handlers if h.is_alive()]

            except socket.timeout:
                continue
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"[!] Lỗi trong vòng lặp chính của server: {e}")
                break
        
        print("[-] Vòng lặp chính của server đã dừng.")

    def stop(self):
        print("\n[*] Đang dừng Caster...")
        self.stop_event.set()
        if self.client_worker and self.client_worker.is_alive():
            print("...Đang dừng Client Worker...")
            self.client_worker.stop()
            self.client_worker.join(timeout=5)
        if self.server_socket:
            print("...Đang đóng Server Socket...")
            self.server_socket.close()
        for handler in self.rover_handlers:
            if handler.is_alive():
                handler.join(timeout=2)
        print("=============================================")
        print("======== NTRIP CASTER ĐÃ DỪNG HẲN ========")
        print("=============================================")

if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        print(f"[!] Lỗi: Không tìm thấy file cấu hình '{CONFIG_FILE}'.")
        exit(1)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[!] Lỗi: File cấu hình '{CONFIG_FILE}' không đúng định dạng JSON. Lỗi: {e}")
        exit(1)
    
    caster = NtripCasterServer(config_data)
    try:
        caster.start()
    except KeyboardInterrupt:
        print("\n[!] Nhận tín hiệu Ctrl+C, đang tắt chương trình...")
    finally:
        caster.stop()