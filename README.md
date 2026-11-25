# VocalForge — Tách Giọng Demucs (File 9+ Tiếng) & Edit Audio Đa Track

Ứng dụng Desktop Windows gọn nhẹ (~25–35 MB cho file `.exe` duy nhất), **không cửa sổ CMD**, gửi cho người khác chỉ cần mở lên dùng ngay:

- **Tự động thiết lập trong giao diện (1 Click):** Lần đầu mở app, nếu máy chưa có `FFmpeg` hoặc môi trường `Demucs AI + PyTorch`, app tự kiểm tra card đồ họa NVIDIA (CUDA) hay CPU rồi tự tải & cài đặt ngay trên giao diện có thanh tiến trình.
- **Tách giọng file dài tới 9+ tiếng không tràn RAM/VRAM:** Tự chia nhỏ thành từng đoạn 5 phút có gối đầu 4 giây (overlap), tách xong đoạn nào giải phóng VRAM & lưu đĩa ngay, hỗ trợ **Tạm dừng / Chạy tiếp** kể cả khi tắt app mở lại.
- **Trình Edit Audio mượt 60fps:** Kéo thả thêm nhiều file audio (nhạc nền, intro, SFX, giọng thu mới), vẽ sóng âm siêu nhẹ (không nạp cả file 9 tiếng vào RAM), cắt đôi (`S`), kéo đầu/đuôi trim, chỉnh Volume / Mute / Solo / Fade In / Fade Out, nghe thử trực tiếp tại mọi mốc giờ và **Xuất File Mix** (`MP3 / FLAC / WAV`).

## Cách đóng gói thành 1 file `VocalForge.exe` gửi cho người khác

### Cách 1: Build ngay trên máy Windows
1. Cài Python 3.11+ (nếu máy build chưa có).
2. Nhấp đúp file **`build_windows.bat`**.
3. Lấy file **`dist\VocalForge.exe`** gửi trực tiếp cho người khác.

> **Lưu trữ gọn 1 chỗ (Mặc định Portable):** Toàn bộ tài nguyên tải về (`FFmpeg`, `Python`, `PyTorch`, `Demucs`, dự án) được lưu ngay trong thư mục **`VocalForgeData\`** nằm cùng chỗ với `VocalForge.exe`. Khi muốn xóa app chỉ cần xóa đúng thư mục đó là sạch 100%.

### Cách 2: Build tự động trên GitHub (không cần cài gì trên máy)
Đẩy thư mục dự án lên GitHub -> vào tab **Actions** -> tải artifact **`VocalForge-Windows-EXE`**.
