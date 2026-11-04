# Native capture apps

RGB MoCap is the clock. Both apps open the address shown in the viewport header (`https://<pc>:8765`). Record on the PC, the Quest, or the iPhone starts the same take.

The Quest app streams face expressions and per-eye gaze. The iPhone app streams full-resolution HEVC. The PC writes `take.mp4` plus the face track.

## Quest

This PC does not have the Android SDK, so the project has to be built on a machine that does.

1. Install Android Studio. Open `native/quest`.
2. Copy `libopenxr_loader.so` from the Meta OpenXR Mobile SDK into `native/quest/app/src/main/jniLibs/arm64-v8a/`.
3. Build the arm64 debug APK and install it with `adb install`.
4. On the headset, allow face tracking and eye tracking for RGB Face.
5. Type the viewport address and tap Enter MR. The right controller A button starts and stops the take.

## iPhone

Open `native/ios/RGBCapture.xcodeproj` on a Mac, sign the target, and run it on the phone. Point it at the same address. The back camera uses the largest format at up to 60 fps.
