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
# Lớp NtripClientWorker (Không thay đổi)
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
# Lớp BaseStationHandler (Không thay đổi)
# ==============================================================================
class BaseStationHandler(threading.Thread):
    def __init__(self, client_socket, address, config, data_queue, on_disconnect_callback):
        super().__init__()
        self.client_socket = client_socket
        self.address = address
        self.config = config
        self.data_queue = data_queue
        self.stop_event = threading.Event()
        self.on_disconnect_callback = on_disconnect_callback
        self.name = f"BaseHandler-{address[0]}:{address[1]}"
        self.daemon = True

    def run(self):
        print(f"[+] Base Station kết nối từ {self.address}. Đang xác thực...")
        try:
            self.client_socket.settimeout(10)
            request_data = self.client_socket.recv(2048).decode(errors='ignore')

            if not request_data.startswith("SOURCE"):
                print(f"[-] Base {self.address}: Yêu cầu không hợp lệ. Chỉ chấp nhận 'SOURCE'.")
                self.client_socket.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nERROR - Use SOURCE method\r\n")
                return

            parts = request_data.split()
            if len(parts) < 2:
                print(f"[-] Base {self.address}: Yêu cầu SOURCE không đầy đủ.")
                self.client_socket.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nERROR - Malformed SOURCE request\r\n")
                return

            source_password = parts[1]
            expected_password = self.config.get("base_source_password")

            if source_password != expected_password:
                print(f"[-] Base {self.address}: Sai mật khẩu nguồn.")
                self.client_socket.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\nERROR - Bad Password\r\n")
                return
            
            print(f"[+] Base {self.address} xác thực thành công. Bắt đầu nhận dữ liệu RTCM.")
            self.client_socket.sendall(b"ICY 200 OK\r\n\r\n")
            
            while not self.data_queue.empty():
                self.data_queue.get()

            self.client_socket.settimeout(30)
            
            while not self.stop_event.is_set():
                data = self.client_socket.recv(4096)
                if not data:
                    print(f"[-] Base {self.address} đã ngắt kết nối.")
                    break
                self.data_queue.put(data)

        except (socket.timeout, IndexError, ValueError):
            print(f"[-] Yêu cầu từ Base {self.address} không hợp lệ hoặc timeout.")
        except Exception as e:
            print(f"[!] Lỗi không xác định trong {self.name}: {e}")
        finally:
            self.client_socket.close()
            self.on_disconnect_callback()
            print(f"[-] Đã đóng kết nối với Base Station {self.address}.")

    def stop(self):
        self.stop_event.set()

# ==============================================================================
# Lớp RoverHandler (Cập nhật)
# ==============================================================================
class RoverHandler(threading.Thread):
    # <<< THAY ĐỔI: Constructor giờ nhận global_rover_accounts thay vì station_config
    def __init__(self, client_socket, address, caster_settings, global_rover_accounts, data_queue):
        super().__init__()
        self.client_socket = client_socket
        self.address = address
        self.caster_settings = caster_settings
        self.rover_accounts = global_rover_accounts # <<< THAY ĐỔI: Sử dụng danh sách tài khoản toàn cục
        self.data_queue = data_queue
        self.stop_event = threading.Event()
        self.name = f"RoverHandler-{address[0]}:{address[1]}"
        self.daemon = True

    # Phương thức _is_authenticated không cần thay đổi, vì nó đã dùng self.rover_accounts
    def _is_authenticated(self, auth_header, mountpoint):
        if mountpoint != f"/{self.caster_settings['mountpoint']}":
            return False, "Bad Mountpoint"
        
        if not auth_header:
            return False, "Authorization header is missing"

        try:
            auth_type, auth_token = auth_header.replace("Authorization: ", "").strip().split()
            if auth_type.lower() != 'basic':
                return False, f"Unsupported Auth type: {auth_type}"
            
            decoded_creds = base64.b64decode(auth_token).decode()
            username, password = decoded_creds.split(':', 1)

            for acc in self.rover_accounts:
                if acc['username'] == username and acc['password'] == password:
                    return True, f"Authenticated as {username}"
            
            return False, f"Invalid credentials for user '{username}'"
        except Exception as e:
            print(f"[!] Lỗi phân tích Auth Header: {e}")
            return False, "Malformed Authorization header"

    def run(self):
        print(f"[+] Rover mới kết nối từ: {self.address}")
        try:
            self.client_socket.settimeout(10)
            request_data = self.client_socket.recv(2048).decode(errors='ignore')
            
            if not request_data:
                print(f"[-] Không nhận được dữ liệu từ {self.address}. Đóng kết nối.")
                return

            headers = request_data.split('\r\n')
            request_line = headers[0]
            method, mountpoint, _ = request_line.split()

            auth_header = next((h for h in headers if h.lower().startswith('authorization:')), None)
            is_auth, reason = self._is_authenticated(auth_header, mountpoint)

            if not is_auth:
                print(f"[-] Rover {self.address} xác thực thất bại: {reason}")
                if reason == "Bad Mountpoint":
                    self.client_socket.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
                else:
                    self.client_socket.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
                return

            print(f"[+] Rover {self.address} xác thực thành công: {reason}. Bắt đầu truyền dữ liệu.")
            self.client_socket.sendall(b"ICY 200 OK\r\n\r\n")
            
            self.client_socket.settimeout(None)
            
            while not self.stop_event.is_set():
                try:
                    rtcm_data = self.data_queue.get(timeout=15)
                    self.client_socket.sendall(rtcm_data)
                except Empty:
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
# Lớp Caster Server chính (Cập nhật)
# ==============================================================================
class NtripCasterServer:
    # <<< THAY ĐỔI: Constructor nhận thêm global_rover_accounts
    def __init__(self, station_config, global_rover_accounts):
        self.config = station_config
        self.caster_settings = station_config['caster_settings']
        self.global_rover_accounts = global_rover_accounts # <<< THAY ĐỔI: Lưu trữ tài khoản toàn cục
        self.rtcm_data_queue = Queue(maxsize=100)
        self.server_socket = None
        self.rover_handlers = []
        self.data_source_worker = None
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

    def _on_base_disconnect(self):
        print("[!] Kết nối từ Base Station đã mất. Caster đang chờ kết nối Base mới.")
        self.data_source_worker = None

    def start(self):
        print("="*45)
        print(f"=== KHỞI ĐỘNG TRẠM: {self.config['name']} ===")
        print(f"=== MODE: {self.config['mode']} ===")
        print("="*45)

        if self.config['mode'] == 'NtripClient':
            self.data_source_worker = NtripClientWorker(self.config['base_connection'], self.rtcm_data_queue)
            self.data_source_worker.start()
        elif self.config['mode'] == 'NtripCaster':
            print("[*] Chế độ NtripCaster: Đang chờ Base Station kết nối và đẩy dữ liệu...")
        else:
            print(f"[!] Lỗi: Mode '{self.config['mode']}' không được hỗ trợ.")
            return

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            host = self.caster_settings['host']
            port = self.caster_settings['port']
            self.server_socket.bind((host, port))
            self.server_socket.listen(10)
            print(f"[+] Caster đang lắng nghe trên {host}:{port} cho các kết nối từ Rover (và Base nếu ở mode NtripCaster)")
        except OSError as e:
            print(f"[!] LỖI NGHIÊM TRỌNG: Không thể bind tới {host}:{port}. Lỗi: {e}")
            self.stop()
            return

        while not self.stop_event.is_set():
            try:
                self.server_socket.settimeout(1.0)
                client_socket, address = self.server_socket.accept()
                
                first_bytes = client_socket.recv(1024, socket.MSG_PEEK)
                request_str = first_bytes.decode(errors='ignore')

                if request_str.startswith('GET / '):
                    self._handle_sourcetable_request(client_socket)
                    continue

                if self.config['mode'] == 'NtripCaster' and request_str.startswith('SOURCE '):
                    if self.data_source_worker and self.data_source_worker.is_alive():
                        print(f"[!] Đã có Base kết nối. Từ chối kết nối Base mới từ {address}")
                        client_socket.sendall(b"HTTP/1.1 409 Conflict\r\n\r\nERROR - Caster already has a source\r\n")
                        client_socket.close()
                    else:
                        self.data_source_worker = BaseStationHandler(client_socket, address, self.config, self.rtcm_data_queue, self._on_base_disconnect)
                        self.data_source_worker.start()
                    continue
                
                # <<< THAY ĐỔI: Truyền danh sách tài khoản toàn cục vào RoverHandler
                handler = RoverHandler(
                    client_socket, 
                    address, 
                    self.caster_settings, 
                    self.global_rover_accounts, 
                    self.rtcm_data_queue
                )
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
        
        if self.data_source_worker and self.data_source_worker.is_alive():
            print(f"...Đang dừng nguồn dữ liệu ({self.data_source_worker.name})...")
            self.data_source_worker.stop()
            self.data_source_worker.join(timeout=5)

        if self.server_socket:
            print("...Đang đóng Server Socket...")
            self.server_socket.close()
            
        for handler in self.rover_handlers:
            if handler.is_alive():
                handler.join(timeout=2)
                
        print("="*45)
        print("======== NTRIP CASTER ĐÃ DỪNG HẲN ========")
        print("="*45)

# ==============================================================================
# Hàm main (Cập nhật)
# ==============================================================================
if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        print(f"[!] Lỗi: Không tìm thấy file cấu hình '{CONFIG_FILE}'.")
        exit(1)
    
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config_data = json.load(f)
            
        # <<< THAY ĐỔI: Đọc cả stations và global_rover_accounts
        stations = config_data.get("stations", [])
        global_accounts = config_data.get("global_rover_accounts", [])
        
        if not stations:
            print(f"[!] Lỗi: File cấu hình '{CONFIG_FILE}' không có trạm nào được định nghĩa trong 'stations'.")
            exit(1)
        if not global_accounts:
            print(f"[!] Cảnh báo: Không tìm thấy tài khoản rover nào trong 'global_rover_accounts'. Sẽ không có rover nào kết nối được.")
            
    except json.JSONDecodeError as e:
        print(f"[!] Lỗi: File cấu hình '{CONFIG_FILE}' không đúng định dạng JSON. Lỗi: {e}")
        exit(1)
    except Exception as e:
        print(f"[!] Lỗi không xác định khi đọc file cấu hình: {e}")
        exit(1)

    print("--- VUI LÒNG CHỌN TRẠM CORS ĐỂ KHỞI ĐỘNG ---")
    for i, station in enumerate(stations):
        print(f"  {i + 1}. {station.get('name', f'Trạm không tên {i+1}')} (Mode: {station.get('mode', 'Chưa rõ')})")
    print("  0. Thoát")
    print("---------------------------------------------")

    choice = -1
    while True:
        try:
            choice_str = input("Nhập lựa chọn của bạn: ")
            choice = int(choice_str)
            if 0 <= choice <= len(stations):
                break
            else:
                print("[!] Lựa chọn không hợp lệ. Vui lòng chọn một số từ danh sách trên.")
        except ValueError:
            print("[!] Vui lòng nhập một con số.")

    if choice == 0:
        print("[-] Đã thoát chương trình.")
        exit(0)
    
    selected_station_config = stations[choice - 1]
    
    caster = None
    try:
        # <<< THAY ĐỔI: Truyền danh sách tài khoản toàn cục khi khởi tạo Caster
        caster = NtripCasterServer(selected_station_config, global_accounts)
        caster.start()
    except KeyboardInterrupt:
        print("\n[!] Nhận tín hiệu Ctrl+C, đang tắt chương trình...")
    except Exception as e:
        print(f"\n[!] Một lỗi nghiêm trọng đã xảy ra: {e}")
    finally:
        if caster:
            caster.stop()