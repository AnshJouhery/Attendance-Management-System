import cv2
import face_recognition
import os
import time
import numpy as np
from datetime import datetime

prev_time = 0

path = "images"

images = []
classNames = []

myList = os.listdir(path)

print("Loading Images...")

for cl in myList:
    curImg = cv2.imread(f'{path}/{cl}')
    images.append(curImg)
    classNames.append(os.path.splitext(cl)[0])

print("Known Persons:", classNames)


def findEncodings(images):
    encodeList = []

    for img in images:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        encodings = face_recognition.face_encodings(img)

        if len(encodings) > 0:
            encodeList.append(encodings[0])

    return encodeList


def markAttendance(name):

    if not os.path.exists("attendance.csv"):
        with open("attendance.csv", "w") as f:
            f.write("Name,Time,Date\n")

    with open("attendance.csv", "r+") as f:

        myDataList = f.readlines()

        nameList = []

        today = datetime.now().strftime("%d-%m-%Y")

        for line in myDataList[1:]:

            entry = line.split(",")

            if len(entry) >= 3:
                stored_name = entry[0]
                stored_date = entry[2].strip()

                if stored_date == today:
                    nameList.append(stored_name)

        if name not in nameList:

            now = datetime.now()

            timeString = now.strftime("%H:%M:%S")
            dateString = now.strftime("%d-%m-%Y")

            f.write(f"{name},{timeString},{dateString}\n")

            print(f"Attendance Marked -> {name}")


print("Encoding Faces...")
encodeListKnown = findEncodings(images)
print("Encoding Complete")

cap = cv2.VideoCapture(0)

while True:

    success, img = cap.read()

    current_time = time.time()
    fps = 1 / (current_time - prev_time)
    prev_time = current_time

    cv2.putText(
        img,
        f"FPS: {int(fps)}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (0, 255, 0),
        2
    )

    if not success:
        break

    imgSmall = cv2.resize(img, (0, 0), None, 0.25, 0.25)
    imgSmall = cv2.cvtColor(imgSmall, cv2.COLOR_BGR2RGB)

    facesCurFrame = face_recognition.face_locations(imgSmall)
    encodesCurFrame = face_recognition.face_encodings(
        imgSmall,
        facesCurFrame
    )

    for encodeFace, faceLoc in zip(
        encodesCurFrame,
        facesCurFrame
    ):

        matches = face_recognition.compare_faces(
            encodeListKnown,
            encodeFace
        )

        faceDis = face_recognition.face_distance(
            encodeListKnown,
            encodeFace
        )

        matchIndex = np.argmin(faceDis)

        if matches[matchIndex]:

            name = classNames[matchIndex].upper()

            y1, x2, y2, x1 = faceLoc

            y1 *= 4
            x2 *= 4
            y2 *= 4
            x1 *= 4

            cv2.rectangle(
                img,
                (x1, y1),
                (x2, y2),
                (0, 255, 0),
                2
            )

            cv2.rectangle(
                img,
                (x1, y2 - 35),
                (x2, y2),
                (0, 255, 0),
                cv2.FILLED
            )

            cv2.putText(
                img,
                name,
                (x1 + 6, y2 - 6),
                cv2.FONT_HERSHEY_COMPLEX,
                1,
                (255, 255, 255),
                2
            )

            markAttendance(name)


        else:

            y1, x2, y2, x1 = faceLoc
            y1 *= 4
            x2 *= 4
            y2 *= 4
            x1 *= 4

            cv2.rectangle(
                img,
                (x1, y1),
                (x2, y2),
                (0, 0, 255),
                2
            )

            cv2.putText(
                img,
                "UNKNOWN",
                (x1, y1 - 10),
                cv2.FONT_HERSHEY_COMPLEX,
                0.8,
                (0, 0, 255),
                2
            )

            filename = f"unknown_faces/unknown_{int(time.time())}.jpg"

            if not os.path.exists(filename):
                cv2.imwrite(filename, img)

                print(f"Unknown face saved: {filename}")

    cv2.imshow("Attendance System", img)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()