import socket
import base64
import time
import json
import os
import threading
from datetime import datetime

# Định nghĩa đường dẫn tệp cấu hình và dữ liệu
CONFIG_FILE = "ntrip_config.json"
PROVINCES_FILE = "provinces.json"

# Định nghĩa dữ liệu tỉnh thành mặc định
DEFAULT_PROVINCES = {
    "Hà Nội": [21.0285, 105.8542],
    "TP. Hồ Chí Minh": [10.7769, 106.7009],
    "Đà Nẵng": [16.0479, 108.2208]
}

# Quản lý các kết nối/threads đang hoạt động
# Sẽ chứa dicts: {'id': str, 'thread': obj, 'name': str, 'province': str, 'stop_event': Event}
running_connection_threads = []
thread_lock = threading.Lock() # Để bảo vệ truy cập vào running_connection_threads
global_conn_counter = 0 # Để tạo ID duy nhất cho mỗi kết nối


# ====== Chuyển đổi tọa độ từ decimal degrees sang NMEA format ======
def convert_to_nmea_format(lat, lon):
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
    return lat_nmea, lat_dir, lon_nmea, lon_dir

# ====== Tạo câu GGA từ tọa độ ======
def generate_gga(lat, lon):
    now = datetime.utcnow()
    time_str = now.strftime("%H%M%S.00")
    lat_nmea, lat_dir, lon_nmea, lon_dir = convert_to_nmea_format(lat, lon)
    gga_body = f"$GPGGA,{time_str},{lat_nmea},{lat_dir},{lon_nmea},{lon_dir},1,12,1.0,10.0,M,0.0,M,,"
    checksum = 0
    for char in gga_body[1:]:
        checksum ^= ord(char)
    gga = f"{gga_body}*{checksum:02X}\r\n"
    return gga

# ====== Tạo yêu cầu kết nối NTRIP ======
def create_ntrip_request(host, mountpoint, username, password):
    credentials = f"{username}:{password}"
    credentials_encoded = base64.b64encode(credentials.encode()).decode()
    request = (
        f"GET /{mountpoint} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Ntrip-Version: Ntrip/2.0\r\n"
        f"User-Agent: NTRIP PythonClient/1.0\r\n"
        f"Authorization: Basic {credentials_encoded}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )
    return request

# ====== Kiểm tra phản hồi kết nối (không print) ======
def check_response_silent(response_bytes):
    if b"ICY 200 OK" in response_bytes or b"HTTP/1.1 200 OK" in response_bytes:
        return True
    return False

# ====== Lưu và đọc cấu hình (Giữ nguyên print cho menu) ======
def save_config(config_data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
        print(f"✅ Đã lưu cấu hình thành công vào {CONFIG_FILE}")
    except IOError as e:
        print(f"❌ Lỗi khi lưu cấu hình vào {CONFIG_FILE}: {e}")

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️ Lỗi giải mã JSON từ tệp cấu hình {CONFIG_FILE}. Tạo cấu hình rỗng.")
            return {"connections": []}
        except IOError as e:
            print(f"❌ Lỗi đọc tệp cấu hình {CONFIG_FILE}: {e}")
            return {"connections": []}
    return {"connections": []}

# ====== Lưu và đọc dữ liệu tỉnh thành (Giữ nguyên print cho menu) ======
def _save_default_provinces():
    try:
        with open(PROVINCES_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_PROVINCES, f, indent=4, ensure_ascii=False)
        print(f"✅ Đã tạo và lưu dữ liệu tỉnh thành mặc định vào {PROVINCES_FILE}")
    except IOError as e:
        print(f"❌ Lỗi khi lưu dữ liệu tỉnh thành vào {PROVINCES_FILE}: {e}")

def load_provinces():
    if os.path.exists(PROVINCES_FILE):
        try:
            with open(PROVINCES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"⚠️ Lỗi giải mã JSON từ tệp {PROVINCES_FILE}. Sử dụng dữ liệu mặc định.")
            _save_default_provinces()
            return DEFAULT_PROVINCES
        except IOError as e:
            print(f"❌ Lỗi đọc tệp dữ liệu tỉnh thành {PROVINCES_FILE}: {e}")
            return DEFAULT_PROVINCES
    else:
        print(f"ℹ️ Không tìm thấy tệp {PROVINCES_FILE}. Tạo mới với dữ liệu mặc định.")
        _save_default_provinces()
        return DEFAULT_PROVINCES

# ====== Quản lý kết nối NTRIP (chạy ẩn, ko log, có stop_event) ======
def connect_ntrip_silent(connection_details, province_name, gga_interval, stop_event: threading.Event, conn_id_str: str):
    host = connection_details["host"]
    port = int(connection_details["port"])
    mountpoint = connection_details["mountpoint"]
    username = connection_details["username"]
    password = connection_details["password"]

    # Sử dụng load_provinces nhưng không in ra lỗi nếu có từ hàm này, vì đây là worker thread
    # Lỗi load_provinces sẽ được xử lý ở menu hoặc khi khởi tạo
    local_provinces_data = {}
    try:
        with open(PROVINCES_FILE, "r", encoding="utf-8") as f:
            local_provinces_data = json.load(f)
    except: # Bất kỳ lỗi nào khi đọc file tỉnh, dùng default (hoặc rỗng nếu muốn chặt chẽ hơn)
        local_provinces_data = DEFAULT_PROVINCES


    if province_name not in local_provinces_data:
        # Worker thread không nên print, lỗi này sẽ khiến thread kết thúc lặng lẽ.
        # print(f"Debug [{conn_id_str}]: Không tìm thấy tỉnh {province_name}")
        return

    lat, lon = local_provinces_data[province_name]
    
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(20.0) # Initial connection timeout
        s.connect((host, port))

        request = create_ntrip_request(host, mountpoint, username, password)
        s.sendall(request.encode())

        response = s.recv(2048)
        if not check_response_silent(response):
            return 

        initial_gga = generate_gga(lat, lon)
        s.sendall(initial_gga.encode())

        last_gga_time = time.time()
        s.settimeout(1.0) # Short timeout for recv to check stop_event frequently

        while not stop_event.is_set():
            current_time = time.time()
            if current_time - last_gga_time >= gga_interval:
                gga_message = generate_gga(lat, lon)
                try:
                    s.sendall(gga_message.encode())
                    last_gga_time = current_time
                except socket.error:
                    break 
            
            try:
                data = s.recv(4096) # Kích thước buffer nhận dữ liệu
                if not data:
                    break 
                # Dữ liệu RTCM đã nhận (biến `data`)
                # Có thể xử lý ở đây: ghi file, chuyển tiếp, etc. (hiện tại bỏ qua)

            except socket.timeout:
                continue # Bình thường, không có dữ liệu, tiếp tục check stop_event/send GGA
            except socket.error:
                break 
            except Exception: # Lỗi không mong muốn khác
                break
    
    except (socket.timeout, socket.error, ConnectionRefusedError):
        pass # Các lỗi kết nối dự kiến, thread sẽ tự kết thúc
    except Exception:
        pass # Các lỗi không mong muốn khác, thread sẽ tự kết thúc
    finally:
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except:
                pass
            s.close()

# ====== Quản lý thông tin kết nối (Menu prints giữ nguyên) ======
def add_connection():
    print("\n=== THÊM THÔNG TIN KẾT NỐI MỚI ===")
    name = input("Tên gợi nhớ cho kết nối: ").strip()
    host = input("Địa chỉ server NTRIP (VD: ntrip.example.com): ").strip()
    port_str = input("Cổng kết nối (VD: 2101): ").strip()
    mountpoint = input("Mountpoint (VD: RTCM3_GPS): ").strip()
    username = input("Tên đăng nhập (bỏ trống nếu không có): ").strip()
    password = input("Mật khẩu (bỏ trống nếu không có): ").strip()

    if not all([name, host, port_str, mountpoint]):
        print("❌ Tên, host, port, và mountpoint không được để trống.")
        return
    try:
        port = int(port_str)
    except ValueError:
        print("❌ Cổng phải là một số nguyên.")
        return

    new_connection = {
        "name": name, "host": host, "port": port,
        "mountpoint": mountpoint, "username": username, "password": password
    }
    config_data = load_config()
    config_data["connections"].append(new_connection)
    save_config(config_data)

def list_connections_and_select():
    config_data = load_config()
    connections = config_data.get("connections", [])
    if not connections:
        print("❌ Chưa có thông tin kết nối nào được lưu.")
        return None
    print("\n=== DANH SÁCH KẾT NỐI ĐÃ LƯU ===")
    for i, conn in enumerate(connections, 1):
        print(f"{i}. {conn['name']} ({conn['host']}:{conn['port']} - {conn['mountpoint']})")
    while True:
        try:
            choice_str = input("\nChọn một kết nối bằng số (nhập 0 để quay lại): ").strip()
            choice = int(choice_str)
            if choice == 0: return None
            if 1 <= choice <= len(connections): return connections[choice - 1]
            else: print("❌ Lựa chọn không hợp lệ.")
        except ValueError: print("❌ Vui lòng nhập một số.")

# ====== Quản lý tỉnh thành (Menu prints giữ nguyên) ======
def list_provinces_and_select():
    provinces_data = load_provinces()
    if not provinces_data:
        print("❌ Không có dữ liệu tỉnh thành nào.")
        return None
    provinces_list = list(provinces_data.keys())
    print("\n=== DANH SÁCH TỈNH THÀNH ===")
    for i, province_name in enumerate(provinces_list, 1):
        coords = provinces_data[province_name]
        print(f"{i}. {province_name} - [{coords[0]}, {coords[1]}]")
    while True:
        try:
            choice_str = input("\nChọn một tỉnh thành bằng số (nhập 0 để quay lại): ").strip()
            choice = int(choice_str)
            if choice == 0: return None
            if 1 <= choice <= len(provinces_list): return provinces_list[choice - 1]
            else: print("❌ Lựa chọn không hợp lệ.")
        except ValueError: print("❌ Vui lòng nhập một số.")

def add_province():
    print("\n=== THÊM TỈNH THÀNH MỚI ===")
    name = input("Tên tỉnh/thành phố: ").strip()
    if not name:
        print("❌ Tên tỉnh thành không được để trống."); return
    try:
        lat = float(input("Vĩ độ (số thập phân, VD: 21.0285): ").strip())
        lon = float(input("Kinh độ (số thập phân, VD: 105.8542): ").strip())
    except ValueError:
        print("❌ Tọa độ không hợp lệ."); return
    provinces_data = load_provinces()
    original_data_for_check = dict(provinces_data) # Tạo bản sao để kiểm tra đã tồn tại chưa
    
    provinces_data[name] = [lat, lon] # Thêm hoặc cập nhật
    
    action_performed = "cập nhật" if name in original_data_for_check else "thêm"

    try:
        with open(PROVINCES_FILE, "w", encoding="utf-8") as f:
            json.dump(provinces_data, f, indent=4, ensure_ascii=False)
        print(f"✅ Đã {action_performed} tỉnh {name} thành công!")
    except IOError as e:
        print(f"❌ Lỗi khi lưu dữ liệu tỉnh thành: {e}")

# ====== Quản lý các thread kết nối đang chạy ======
def manage_running_connections():
    global running_connection_threads
    
    cleaned_list = []
    with thread_lock:
        for conn_info in running_connection_threads:
            if conn_info['thread'].is_alive():
                cleaned_list.append(conn_info)
        running_connection_threads = cleaned_list

    print("\n--- Quản Lý Kết Nối Đang Chạy ---")
    if not running_connection_threads:
        print("ℹ️ Không có kết nối nào đang hoạt động.")
        return

    for i, conn_info in enumerate(running_connection_threads):
        print(f"{i+1}. {conn_info['name']} ({conn_info['province']}) - ID: {conn_info['id']}")

    try:
        choice_str = input("Nhập số thứ tự của kết nối để dừng (hoặc 0 để quay lại): ").strip()
        choice = int(choice_str)
        if choice == 0:
            return
        if 1 <= choice <= len(running_connection_threads):
            selected_conn_info = running_connection_threads[choice-1] # Lấy trước khi có thể bị remove
            print(f"🛑 Đang yêu cầu dừng kết nối {selected_conn_info['name']} (ID: {selected_conn_info['id']})...")
            selected_conn_info['stop_event'].set()
            # Thread sẽ tự dọn dẹp và thoát, không cần join ở đây để tránh block menu
        else:
            print("❌ Lựa chọn không hợp lệ.")
    except (ValueError, IndexError):
        print("❌ Lựa chọn không hợp lệ.")


# ====== Menu chính ======
def main_menu():
    global global_conn_counter
    # Tạo file config và provinces với giá trị mặc định nếu chưa có
    if not os.path.exists(CONFIG_FILE): save_config({"connections": []})
    if not os.path.exists(PROVINCES_FILE): _save_default_provinces()

    while True:
        print("\n====== PHẦN MỀM KẾT NỐI NTRIP ======")
        print("1. Bắt đầu kết nối NTRIP mới ( chạy ngầm )")
        print("2. Quản lý thông tin kết nối đã lưu")
        print("3. Quản lý danh sách tỉnh thành")
        print("4. Quản lý các kết nối đang chạy")
        print("0. Thoát")

        choice = input("\nNhập lựa chọn của bạn: ").strip()

        if choice == "1":
            selected_connection = list_connections_and_select()
            if selected_connection:
                selected_province_name = list_provinces_and_select()
                if selected_province_name:
                    try:
                        gga_interval_str = input("Nhập khoảng thời gian gửi GGA (giây, mặc định 10, tối thiểu 5): ").strip()
                        gga_interval = int(gga_interval_str) if gga_interval_str else 10
                        if gga_interval < 5 : gga_interval = 5

                        with thread_lock: # Bảo vệ global_conn_counter
                            global_conn_counter += 1
                            conn_id = f"NTRIP-{global_conn_counter}"
                        
                        stop_event = threading.Event()
                        thread = threading.Thread(
                            target=connect_ntrip_silent,
                            args=(selected_connection, selected_province_name, gga_interval, stop_event, conn_id),
                            daemon=True 
                        )
                        thread.start()

                        with thread_lock:
                            running_connection_threads.append({
                                'id': conn_id,
                                'thread': thread,
                                'name': selected_connection['name'],
                                'province': selected_province_name,
                                'stop_event': stop_event
                            })
                        print(f"✅ Đã bắt đầu kết nối ngầm cho '{selected_connection['name']}' tại '{selected_province_name}' (ID: {conn_id}).")

                    except ValueError:
                        print("❌ Khoảng thời gian không hợp lệ.")
        elif choice == "2":
            while True:
                print("\n--- Quản lý Thông Tin Kết Nối Đã Lưu ---")
                print("1. Thêm kết nối mới")
                print("2. Xem danh sách kết nối")
                print("0. Quay lại menu chính")
                subchoice = input("Nhập lựa chọn: ").strip()
                if subchoice == "1": add_connection()
                elif subchoice == "2": list_connections_and_select()
                elif subchoice == "0": break
                else: print("❌ Lựa chọn không hợp lệ.")
        elif choice == "3":
            while True:
                print("\n--- Quản lý Danh Sách Tỉnh Thành ---")
                print("1. Thêm tỉnh thành mới (hoặc cập nhật)")
                print("2. Xem danh sách tỉnh thành")
                print("0. Quay lại menu chính")
                subchoice = input("Nhập lựa chọn: ").strip()
                if subchoice == "1": add_province()
                elif subchoice == "2": list_provinces_and_select()
                elif subchoice == "0": break
                else: print("❌ Lựa chọn không hợp lệ.")
        elif choice == "4":
            manage_running_connections()
        elif choice == "0":
            shutdown_all_connections()
            print("👋 Tạm biệt!")
            break
        else:
            print("❌ Lựa chọn không hợp lệ! Vui lòng chọn lại.")

def shutdown_all_connections(wait_timeout=2.0):
    """Yêu cầu dừng tất cả các thread kết nối và chờ chúng kết thúc."""
    print("👋 Đang yêu cầu dừng tất cả các kết nối ngầm...")
    
    threads_to_wait_for = []
    with thread_lock:
        for conn_info in running_connection_threads:
            if conn_info['thread'].is_alive():
                conn_info['stop_event'].set()
                threads_to_wait_for.append(conn_info['thread'])
    
    if not threads_to_wait_for:
        print("ℹ️ Không có kết nối nào đang hoạt động để dừng.")
        return

    print(f"⏳ Đang chờ các kết nối dừng (tối đa {wait_timeout} giây mỗi kết nối)...")
    for t in threads_to_wait_for:
        t.join(timeout=wait_timeout)
    
    # Dọn dẹp danh sách lần cuối
    final_alive_threads = []
    with thread_lock:
        for conn_info in running_connection_threads:
            if conn_info['thread'].is_alive():
                final_alive_threads.append(conn_info)
        running_connection_threads = final_alive_threads # Cập nhật lại list toàn cục

    if running_connection_threads:
        print(f"⚠️ {len(running_connection_threads)} kết nối có thể chưa dừng hoàn toàn.")
    else:
        print("✅ Tất cả các kết nối ngầm đã được xử lý dừng.")


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\nℹ️ Nhận tín hiệu dừng chương trình (Ctrl+C).")
        shutdown_all_connections(wait_timeout=1.0) # Cố gắng dừng nhanh hơn khi bị Ctrl+C
        print("✅ Chương trình đã thoát.")
    except Exception as e:
        print(f"🆘 Lỗi không mong muốn ở tầng cao nhất: {e}")
        shutdown_all_connections(wait_timeout=1.0)
        print("✅ Chương trình đã thoát (sau lỗi).")