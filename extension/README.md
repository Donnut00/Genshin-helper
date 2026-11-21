# Genshin Helper — Browser Extension Control Center (Manifest V3, v4.5.0)

Tiện ích mở rộng cho **Google Chrome** và **Microsoft Edge**, đóng vai trò trung tâm điều khiển (**Extension Control Center**) và đồng bộ vị trí Genshin Impact lên HoYoLAB Interactive Map ổn định hơn so với Userscript:

- **Bề mặt bản đồ sạch 100% (Zero-Clutter Map Surface):** Từ phiên bản `v4.5.0`, toàn bộ bảng điều khiển, công tắc chế độ, cài đặt phím tắt và chụp chẩn đoán đều nằm gọn trong **Extension Popup** trên thanh công cụ trình duyệt. Khung widget nổi trên bản đồ đã được gỡ bỏ — chỉ giữ lại mũi tên vị trí người chơi, thông báo pill thoáng qua tự ẩn (`2.4s`), và hộp thoại ghi chú khi chụp chẩn đoán.
- **Không bị Chrome giảm nhịp (Zero-Throttle):** Vòng lặp `/poll?since=<seq>` chạy trong Service Worker nền (`background.js`), nên vẫn đồng bộ tức thời ngay cả khi tab HoYoLAB Map đang bị che khuất bởi game trên 1 màn hình.
- **Tự động nhường quyền an toàn:** Extension gắn cờ `window.__gh_runtime_mode = 'extension'` để Userscript Tampermonkey (nếu còn bật) tự thoát và không chạy chồng chéo.

---

## 1. Các trạng thái nguồn & Huy hiệu thanh công cụ

Huy hiệu trên biểu tượng **Genshin Helper** phản ánh tức thời trạng thái Desktop App:

| Huy hiệu | Trạng thái | Ý nghĩa |
| :--- | :--- | :--- |
| **`ON`** (Xanh lá) | **Active** | Desktop Engine đang chạy bình thường, tự động định vị & lắng nghe phím tắt. |
| **`ZZZ`** (Vàng hổ phách) | **Standby** | Engine tạm nghỉ (0% CPU) nhưng vẫn giữ server cục bộ nhẹ để bạn bấm **BẬT** lại ngay lập tức trong popup. |
| **`OFF`** (Đỏ) | **Offline** | Desktop App đã tắt hoàn toàn. Nhấn **BẬT** trong popup để tự khởi động qua Native Host (`com.genshinhelper.host`). |

- Để đóng hoàn toàn tiến trình nền và biểu tượng khay hệ thống (System Tray), mở tab **⌨️ Phím tắt & Debug** trong Popup và chọn **⏻ Thoát hẳn Desktop App**.

---

## 2. Hướng dẫn cài đặt (Setup Installer hoặc Load Unpacked)

### Cách 1: Cài đặt qua Bộ cài Setup đi kèm (Khuyên dùng)
- Bộ cài Setup tự động cấu hình Extension ID cố định (`nfkijdoblmphmdljfogooajhbldkeojh`) và đăng ký **Native Messaging Host** (`com.genshinhelper.host`), giúp nút **BẬT Server** trong Popup có thể tự khởi chạy Desktop App ngay cả khi app đang tắt hoàn toàn.

### Cách 2: Cài đặt thủ công (Off-Store Load Unpacked)
#### Google Chrome
1. Mở Chrome và truy cập `chrome://extensions`.
2. Bật công tắc **Developer mode** (Chế độ dành cho nhà phát triển) ở góc trên bên phải.
3. Nhấn **Load unpacked** (Tải tiện ích đã giải nén).
4. Chọn thư mục **`extension/`** bên trong dự án `Genshin-Helper` (ID tự động giữ cố định nhờ trường `key` trong `manifest.json`).
5. Ghim biểu tượng **Genshin Helper** lên thanh công cụ để theo dõi `ON`/`ZZZ`/`OFF` và mở Control Center.

#### Microsoft Edge
1. Mở Edge và truy cập `edge://extensions`.
2. Bật công tắc **Developer mode** (Chế độ nhà phát triển).
3. Nhấn **Load unpacked** (Tải phần mở rộng đã giải nén) và chọn thư mục **`extension/`**.

> **Lưu ý:** Khi cài đặt thủ công (Load Unpacked) mà chưa chạy Bộ cài Setup để đăng ký Native Host, nút BẬT sẽ chỉ đánh thức được app khi app đang ở chế độ **Standby (`ZZZ`)**; nếu app đang **Offline (`OFF`)**, bạn cần mở `GenshinHelper` trên máy hoặc chạy Bộ cài Setup.

---

## 3. Lưu ý khi chuyển từ Tampermonkey sang Extension

- Hãy **tắt hoặc gỡ** Userscript `HoYoLAB Genshin Helper` (và `Live Dev Loader` nếu có) trong bảng điều khiển Tampermonkey để tránh tải trùng lặp và giữ trình duyệt gọn nhẹ.
- Sau khi cài đặt hoặc cập nhật extension, hãy nhấn **F5 / Tải lại** các tab HoYoLAB Interactive Map đang mở sẵn để nội dung script mới được nạp vào trang.

---

## 4. Điều khiển & Chế độ 1 Màn Hình (v4.5.0)

1. Mở **Extension Popup** → tab **🎛️ Điều khiển** để bật/tắt Engine, xem tọa độ **Đồng bộ gần nhất**, bật **Tự đồng bộ Map (M)** hoặc **Chế độ 1 Màn Hình**.
2. Khi chơi trên 1 màn hình, đặt game ở chế độ **Cửa sổ không viền (Borderless Windowed)** để **HUD Toast** của Desktop App hiển thị nổi trực tiếp trên màn hình game.
3. Mở tab **⌨️ Phím tắt & Debug** để tùy chỉnh phím tắt (hoặc chọn nhanh nút chuột `Mouse 4`, `Mouse 5`, `Chuột giữa`) cho **Sync & Ghim**, **Chuyển nhanh Game ↔ Map**, **Chụp Chẩn Đoán** và lưu bằng **Lưu phím tắt**.
