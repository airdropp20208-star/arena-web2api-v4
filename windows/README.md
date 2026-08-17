# Windows Desktop mode

Thư mục `windows` bổ sung chế độ chạy Desktop cho `arena-web2api-v4` và giữ nguyên các script Android/Termux hiện có. Launcher khởi động gateway FastAPI, kiểm tra 9Router, rồi mở một Chrome profile riêng có extension Arena Token Broker.

Chỉ cần chạy `start-arena-desktop.cmd`. Mặc định gateway dùng `127.0.0.1:8010`, HTTP broker dùng cổng `8765`, và 9Router được kiểm tra tại `127.0.0.1:20128`. Chrome profile riêng nằm dưới `AppData\Local\ArenaDesktop\ChromeProfile`, vì vậy launcher không đóng hoặc sửa profile Chrome cá nhân.

Gateway vẫn đọc cookie từ `.env` cục bộ. Launcher không in cookie, API key hoặc nội dung request. Nếu extension chưa poll trong 30 giây, gateway vẫn được giữ lại và launcher chỉ hiện chẩn đoán an toàn. Chạy `stop-arena-desktop.cmd` để dừng gateway và đúng Chrome profile riêng.

Nếu Python Hermes không nằm trong PATH, đặt biến môi trường `HERMES_PYTHON` trỏ tới `hermes-agent\venv\Scripts\python.exe`.
