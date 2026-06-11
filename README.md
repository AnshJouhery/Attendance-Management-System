# 📸 Face Recognition Attendance Management System

🚀 A smart attendance management system developed using Python, OpenCV, and the Face Recognition library to automate attendance tracking through real-time facial recognition.

The system captures live video from a webcam, identifies registered individuals, and automatically records attendance with accurate date and time stamps. By eliminating manual attendance processes, the system improves efficiency, accuracy, and reliability. It also prevents duplicate attendance entries for the same person on the same day and detects unknown individuals for further verification.

## ✨ Features

✅ Real-Time Face Detection & Recognition
✅ Automatic Attendance Recording
✅ Date & Time Based Logging
✅ Duplicate Attendance Prevention
✅ Unknown Face Detection & Storage
✅ CSV-Based Attendance Management
✅ Live Webcam Integration
✅ Performance Monitoring (FPS Display)
✅ Contactless Attendance Tracking
✅ Easy Registration of New Individuals

## 🛠️ Technologies Used

🐍 Python
📷 OpenCV
👤 Face Recognition Library
📊 NumPy
📁 CSV File Handling

## ⚙️ How It Works

1️⃣ Add images of registered individuals to the **images/** folder. Each image filename is treated as the person's name.

2️⃣ Run the Python application.

3️⃣ The system loads the images and generates facial encodings for all registered individuals.

4️⃣ The webcam starts capturing live video and continuously scans for faces.

5️⃣ When a face is detected, the system compares it with the registered faces stored in the **images/** folder.

6️⃣ If a match is found:

* The person's name is displayed on the screen.
* Attendance is automatically marked.
* The attendance record is saved in **attendance.csv** with the current date and time.
* Duplicate attendance entries for the same day are prevented.

7️⃣ If no match is found:

* The face is labeled as **UNKNOWN**.
* The captured image is stored in the **unknown_faces/** folder for future review and verification.

## 📂 Output Files

📄 **attendance.csv** – Stores attendance records with Name, Time, and Date.
📁 **unknown_faces/** – Stores images of unrecognized individuals detected by the system.

## 🎯 Objective

To automate attendance tracking using facial recognition technology, reducing manual effort, improving accuracy, and providing a simple and efficient attendance management solution.
