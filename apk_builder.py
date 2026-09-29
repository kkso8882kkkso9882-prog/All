#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "apk_builder_logs"
WORK_DIR = SCRIPT_DIR / "apk_builder_work"
OUT_DIR = SCRIPT_DIR / "apk_output"

IS_WIN = os.name == "nt"

IGNORED = {".gradle", ".git", "node_modules", "__MACOSX"}

ANDROID_DOWNLOAD = Path("/storage/emulated/0/Download")
ANDROID_DOWNLOAD_LOWER = Path("/storage/emulated/0/download")
TERMUX_DOWNLOAD = Path.home() / "storage" / "downloads"


def banner(text):
    print("\n" + "=" * 60 + f"\n{text}\n" + "=" * 60, flush=True)


def command_exists(command):
    return shutil.which(command) is not None


# ============================================================
# TERMUX DEPENDENCIES
# ============================================================

def is_termux():
    return "TERMUX_VERSION" in os.environ or Path("/data/data/com.termux").exists()


def termux_pkg_available():
    return command_exists("pkg") or command_exists("apt")


def install_termux_package(package):
    if not is_termux():
        return False
    if not termux_pkg_available():
        print("[WARN] ไม่พบ pkg/apt ของ Termux")
        return False

    print(f"[..] กำลังติดตั้ง Termux package: {package}")
    if command_exists("pkg"):
        command = ["pkg", "install", "-y", package]
    else:
        command = ["apt", "install", "-y", package]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        print(result.stdout)
        return result.returncode == 0
    except Exception as error:
        print(f"[ERROR] ติดตั้ง {package} ไม่สำเร็จ: {error}")
        return False


def ensure_python():
    if command_exists("python"):
        print(f"[OK] Python: {shutil.which('python')}")
        return True
    print("[MISS] Python")
    if is_termux():
        return install_termux_package("python")
    print("[ERROR] ไม่สามารถติดตั้ง Python อัตโนมัติบนระบบนี้")
    return False


def ensure_basic_tools():
    if not is_termux():
        return
    packages = []
    if not command_exists("unzip"):
        packages.append("unzip")
    if not command_exists("curl"):
        packages.append("curl")
    for package in packages:
        install_termux_package(package)


# ============================================================
# STORAGE
# ============================================================

def storage_locations():
    locations = [
        ANDROID_DOWNLOAD,
        ANDROID_DOWNLOAD_LOWER,
        TERMUX_DOWNLOAD,
        Path.home() / "Downloads",
        Path.cwd(),
        SCRIPT_DIR,
    ]
    result = []
    seen = set()
    for path in locations:
        try:
            path = path.resolve()
        except Exception:
            pass
        if str(path) not in seen:
            seen.add(str(path))
            result.append(path)
    return result


def show_storage_locations():
    banner("STORAGE CHECK")
    for path in storage_locations():
        if path.exists():
            print(f"[OK] {path}")
        else:
            print(f"[MISS] {path}")


# ============================================================
# ZIP SELECTION
# ============================================================

def find_zips():
    zips = []
    seen = set()
    for directory in storage_locations():
        if not directory.is_dir():
            continue
        try:
            for path in directory.glob("*.zip"):
                try:
                    resolved = path.resolve()
                except Exception:
                    resolved = path
                if resolved in seen:
                    continue
                seen.add(resolved)
                zips.append(resolved)
        except Exception:
            pass
    zips.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[:30]


def pick_zip():
    banner("SEARCHING FOR ANDROID PROJECT ZIP")
    zips = find_zips()

    if zips:
        for index, path in enumerate(zips, 1):
            try:
                size = path.stat().st_size / 1024 / 1024
            except Exception:
                size = 0
            print(f" [{index}] {path} ({size:.1f} MB)")
    else:
        print("[MISS] ไม่พบ ZIP ใน Download")

    try:
        choice = input(
            "\nเลือกหมายเลข ZIP หรือพิมพ์ path เอง (Enter = ยกเลิก): "
        ).strip().strip("\"'")
    except EOFError:
        return None

    if not choice:
        return None

    if choice.isdigit():
        index = int(choice)
        if 1 <= index <= len(zips):
            return zips[index - 1]
        print("[ERROR] หมายเลขไม่ถูกต้อง")
        return None

    path = Path(choice).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists():
        print(f"[ERROR] ไม่พบไฟล์: {path}")
        return None
    return path


# ============================================================
# ZIP EXTRACTION
# ============================================================

def extract_zip(zip_path):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    destination = WORK_DIR / f"{zip_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}"
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    banner(f"EXTRACT: {zip_path.name}")

    with zipfile.ZipFile(zip_path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ไฟล์ ZIP เสียหาย: {bad}")

        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"path อันตรายใน ZIP: {member.filename}")

        archive.extractall(destination)

    print(f"[OK] แตก ZIP ไปที่:\n{destination}")
    return destination


# ============================================================
# PROJECT DETECTION
# ============================================================

def find_project_root(base):
    base = Path(base).resolve()
    candidates = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in IGNORED]
        files_set = set(files)
        if "settings.gradle" in files_set or "settings.gradle.kts" in files_set:
            candidates.append(Path(root))
            continue
        if "gradlew" in files_set and (
            "build.gradle" in files_set or "build.gradle.kts" in files_set
        ):
            candidates.append(Path(root))

    if not candidates:
        return None
    candidates.sort(key=lambda p: len(p.relative_to(base).parts))
    return candidates[0]


def looks_like_android_project(path):
    path = Path(path)
    if not path.is_dir():
        return False
    return (
        (path / "settings.gradle").exists()
        or (path / "settings.gradle.kts").exists()
        or (
            (path / "gradlew").exists()
            and (
                (path / "build.gradle").exists()
                or (path / "build.gradle.kts").exists()
            )
        )
    )


def find_project_from_download():
    banner("SEARCHING FOR ANDROID PROJECT")
    for directory in storage_locations():
        if not directory.is_dir():
            continue
        try:
            if looks_like_android_project(directory):
                return directory
            for child in directory.iterdir():
                if child.is_dir() and looks_like_android_project(child):
                    return child
        except Exception:
            pass
    return None


# ============================================================
# SDK
# ============================================================

def find_sdk():
    environment_paths = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
    ]
    for value in environment_paths:
        if value:
            path = Path(value).expanduser()
            if path.is_dir():
                return path.resolve()

    candidates = [
        Path.home() / "Android" / "Sdk",
        Path.home() / "Android" / "sdk",
        Path.home() / ".android" / "sdk",
        Path.home() / "Library" / "Android" / "sdk",
        Path("/data/data/com.termux/files/home/Android/Sdk"),
        Path("/data/data/com.termux/files/home/.android/sdk"),
        Path("/storage/emulated/0/Android/Sdk"),
    ]
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    return None


def find_sdkmanager(sdk):
    found = shutil.which("sdkmanager")
    if found:
        return found
    if not sdk:
        return None

    executable = "sdkmanager.bat" if IS_WIN else "sdkmanager"
    cmdline = sdk / "cmdline-tools"
    if cmdline.exists():
        versions = sorted(cmdline.glob(f"*/bin/{executable}"), reverse=True)
        if versions:
            return str(versions[0])

    old = sdk / "tools" / "bin" / executable
    if old.exists():
        return str(old)
    return None


# ============================================================
# BUILDER
# ============================================================

class Builder:
    def __init__(self, project, label, no_install=False, clean=False, release=False):
        self.project = Path(project).resolve()
        self.label = label
        self.no_install = no_install
        self.clean = clean
        self.release = release
        self.sdk = None
        self.start = time.time()
        self.last_output = ""

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------

    def run(self, command, cwd=None, timeout=None, stdin_text=None, quiet=False):
        command = [str(value) for value in command]
        if not quiet:
            print("\n>>> " + " ".join(command), flush=True)

        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd or self.project),
                stdin=(subprocess.PIPE if stdin_text else subprocess.DEVNULL),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            print("!!! ไม่พบคำสั่งนี้")
            return -127, ""
        except OSError as error:
            print(f"!!! รันไม่ได้: {error}")
            return -1, str(error)

        timed_out = {"value": False}

        def kill_process():
            timed_out["value"] = True
            try:
                process.kill()
            except Exception:
                pass

        timer = None
        if timeout:
            timer = threading.Timer(timeout, kill_process)
            timer.start()

        output_lines = []
        try:
            if stdin_text:
                try:
                    process.stdin.write(stdin_text)
                    process.stdin.close()
                except OSError:
                    pass

            for line in process.stdout:
                output_lines.append(line)
                if not quiet:
                    print(line, end="", flush=True)

            process.wait()
        except KeyboardInterrupt:
            try:
                process.kill()
            except Exception:
                pass
            raise
        finally:
            if timer:
                timer.cancel()

        output = "".join(output_lines)
        if timed_out["value"]:
            print("!!! Command timed out")
            return -2, output
        return process.returncode, output

    # --------------------------------------------------------

    def check_environment(self):
        banner("ENVIRONMENT CHECK")
        print(f"Project: {self.project}")
        print(f"Python: {sys.version.split()[0]}")

        java = shutil.which("java")
        java_home = os.environ.get("JAVA_HOME")
        if not java and not java_home:
            print("[MISS] Java JDK")
            if is_termux():
                print("[..] กำลังติดตั้ง OpenJDK 17...")
                if not install_termux_package("openjdk-17"):
                    print("[ERROR] ติดตั้ง OpenJDK 17 ไม่สำเร็จ")
                    return False
                java = shutil.which("java")

        if not java and not java_home:
            print("[ERROR] ไม่พบ Java")
            return False

        print(f"[OK] Java: {java or java_home}")

        for command in ("gradle", "adb"):
            location = shutil.which(command)
            if location:
                print(f"[OK] {command}: {location}")
            else:
                print(f"[MISS] {command}")

        self.sdk = find_sdk()
        if not self.sdk:
            print("[MISS] Android SDK")
            print("[INFO] ตัว Builder ไม่สร้าง Android SDK ปลอมให้ "
                  "เพราะ SDK ต้องมี platform/build-tools ที่ตรงกับโปรเจกต์")
            return False

        print(f"[OK] Android SDK: {self.sdk}")
        return True

    # --------------------------------------------------------

    def prepare_project(self):
        banner("PREPARE PROJECT")
        os.environ["ANDROID_HOME"] = str(self.sdk)
        os.environ.setdefault("ANDROID_SDK_ROOT", str(self.sdk))

        local_properties = self.project / "local.properties"
        old_lines = []
        if local_properties.exists():
            try:
                old_lines = [
                    line
                    for line in local_properties.read_text(errors="replace").splitlines()
                    if not line.strip().startswith("sdk.dir")
                ]
            except Exception:
                old_lines = []
        old_lines.append("sdk.dir=" + self.sdk.as_posix())
        local_properties.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
        print("[OK] local.properties")

        gradlew = self.project / "gradlew"
        if gradlew.exists():
            data = gradlew.read_bytes()
            if b"\r\n" in data:
                gradlew.write_bytes(data.replace(b"\r\n", b"\n"))
                print("[OK] CRLF -> LF")
            try:
                gradlew.chmod(gradlew.stat().st_mode | 0o111)
                print("[OK] gradlew executable")
            except Exception as error:
                print(f"[WARN] chmod: {error}")

    # --------------------------------------------------------

    def wrapper_cmd(self):
        filename = "gradlew.bat" if IS_WIN else "gradlew"
        wrapper = self.project / filename
        wrapper_jar = self.project / "gradle" / "wrapper" / "gradle-wrapper.jar"

        if wrapper.exists() and wrapper_jar.exists():
            return [str(wrapper)]
        if wrapper.exists():
            print("[WARN] พบ gradlew แต่ไม่มี gradle-wrapper.jar")
            return None
        return None

    # --------------------------------------------------------

    def build_attempt(self, name, command):
        banner(f"BUILD ATTEMPT: {name}")
        started = time.time()
        code, output = self.run(command, cwd=self.project, timeout=30 * 60)
        self.last_output = output

        logfile = LOG_DIR / f"{self.label}_{name}.log"
        try:
            logfile.write_text(output, encoding="utf-8")
            print(f"[LOG] {logfile}")
        except Exception as error:
            print(f"[WARN] เซฟ log ไม่ได้: {error}")

        print(
            "Result: "
            + ("SUCCESS" if code == 0 else "FAILED")
            + f" (exit={code}, {time.time() - started:.1f}s)"
        )
        return code == 0

    # --------------------------------------------------------

    def find_apk(self):
        found = []
        for apk in self.project.rglob("*.apk"):
            try:
                relative = apk.relative_to(self.project).parts
            except ValueError:
                continue
            if any(part in IGNORED for part in relative):
                continue
            try:
                if apk.stat().st_size <= 0:
                    continue
            except Exception:
                continue
            found.append(apk)

        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return found

    # --------------------------------------------------------

    def copy_apk(self, apk):
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        output_name = f"{self.label}_{apk.name}"
        destination = OUT_DIR / output_name
        try:
            shutil.copy2(apk, destination)
            print("\n[OK] APK พร้อมใช้งาน:")
            print(f" {destination}")
            try:
                size = destination.stat().st_size / 1024 / 1024
                print(f" Size: {size:.2f} MB")
            except Exception:
                pass
            return destination
        except Exception as error:
            print(f"[ERROR] คัดลอก APK ไม่สำเร็จ: {error}")
            return None

    # --------------------------------------------------------

    def find_adb(self):
        adb = shutil.which("adb")
        if adb:
            return adb
        if not self.sdk:
            return None
        candidates = [
            self.sdk / "platform-tools" / "adb",
            self.sdk / "platform-tools" / "adb.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    # --------------------------------------------------------

    def install_apk(self, apk):
        if self.no_install:
            print("[INFO] ข้ามการติดตั้ง เพราะใช้ --no-install")
            return True

        adb = self.find_adb()
        if not adb:
            print("\n[WARN] ไม่พบ adb")
            print("[INFO] Build APK สำเร็จแล้ว แต่ยังติดตั้งอัตโนมัติไม่ได้")
            print(f"[APK] {apk}")
            return False

        banner("INSTALL APK")
        print(f"[ADB] {adb}")

        code, output = self.run([adb, "devices"], cwd=self.project, timeout=30)
        if code != 0:
            print("[WARN] adb devices ทำงานไม่สำเร็จ")
            return False
        print(output)

        devices = []
        for line in output.splitlines():
            line = line.strip()
            if line and not line.startswith("List of devices"):
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])

        if not devices:
            print("[WARN] ไม่พบอุปกรณ์ Android ที่พร้อมติดตั้งผ่าน ADB")
            print("[INFO] APK ถูกสร้างไว้ที่:")
            print(f" {apk}")
            return False

        print(f"[OK] พบอุปกรณ์: {', '.join(devices)}")

        code, output = self.run(
            [adb, "install", "-r", str(apk)], cwd=self.project, timeout=10 * 60
        )
        if code == 0:
            print("\n[SUCCESS] ติดตั้ง APK สำเร็จ")
            return True

        print("\n[WARN] ติดตั้ง APK ไม่สำเร็จ")
        print(output)
        return False

    # --------------------------------------------------------

    def build_commands(self):
        commands = []
        wrapper = self.wrapper_cmd()
        if wrapper:
            commands.append(("wrapper-debug", wrapper + ["assembleDebug"]))
            commands.append(
                ("wrapper-debug-no-daemon", wrapper + ["--no-daemon", "assembleDebug"])
            )
            if self.release:
                commands.append(("wrapper-release", wrapper + ["assembleRelease"]))
                commands.append(
                    (
                        "wrapper-release-no-daemon",
                        wrapper + ["--no-daemon", "assembleRelease"],
                    )
                )

        gradle = shutil.which("gradle")
        if gradle:
            commands.append(("gradle-debug", [gradle, "assembleDebug"]))
            commands.append(
                ("gradle-debug-no-daemon", [gradle, "--no-daemon", "assembleDebug"])
            )
            if self.release:
                commands.append(("gradle-release", [gradle, "assembleRelease"]))
                commands.append(
                    (
                        "gradle-release-no-daemon",
                        [gradle, "--no-daemon", "assembleRelease"],
                    )
                )
        return commands

    # --------------------------------------------------------

    def clean_project(self):
        if not self.clean:
            return True

        banner("CLEAN BUILD")
        commands = []
        wrapper = self.wrapper_cmd()
        if wrapper:
            commands.append(("wrapper-clean", wrapper + ["clean"]))
        gradle = shutil.which("gradle")
        if gradle:
            commands.append(("gradle-clean", [gradle, "clean"]))

        if not commands:
            print("[WARN] ไม่มี Gradle สำหรับ clean")
            return False

        for name, command in commands:
            if self.build_attempt(name, command):
                return True
        return False

    # --------------------------------------------------------

    def build(self):
        banner("BUILD START")
        if not self.check_environment():
            return None

        self.prepare_project()

        if self.clean:
            self.clean_project()

        commands = self.build_commands()
        if not commands:
            print("[ERROR] ไม่พบ Gradle หรือ Gradle Wrapper")
            return None

        success = False
        for name, command in commands:
            print(f"\n[TRY] {name}")
            if self.build_attempt(name, command):
                success = True
                break

        if not success:
            banner("ALL BUILD METHODS FAILED")
            print("[ERROR] ทุกวิธี Build ล้มเหลว")
            if self.last_output:
                print("\n----- LAST BUILD OUTPUT -----")
                print(self.last_output[-12000:])
                print("----- END OUTPUT -----")
            return None

        banner("SEARCHING FOR APK")
        apks = self.find_apk()
        if not apks:
            print("[ERROR] Build สำเร็จ แต่ไม่พบไฟล์ APK")
            return None

        print(f"[OK] พบ APK {len(apks)} ไฟล์")
        for index, apk in enumerate(apks, 1):
            try:
                size = apk.stat().st_size / 1024 / 1024
            except Exception:
                size = 0
            print(f" [{index}] {apk} ({size:.2f} MB)")

        apk = apks[0]
        print("\n[SELECT] ใช้ APK ล่าสุด:")
        print(f" {apk}")

        output_apk = self.copy_apk(apk)
        if not output_apk:
            return None

        self.install_apk(output_apk)

        elapsed = time.time() - self.start
        banner("BUILD FINISHED")
        print(f"[OK] ใช้เวลา {elapsed:.1f} วินาที")
        print(f"[APK] {output_apk}")
        return output_apk


# ============================================================
# SOURCE RESOLUTION
# ============================================================

def resolve_source(source):
    if source:
        path = Path(source).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"ไม่พบ source: {path}")
        return path

    zips = find_zips()
    if len(zips) == 1:
        print("[AUTO] พบ ZIP เดียว:")
        print(f" {zips[0]}")
        return zips[0]
    if len(zips) > 1:
        selected = pick_zip()
        if selected:
            return selected
        raise RuntimeError("ไม่ได้เลือก ZIP")

    project = find_project_from_download()
    if project:
        print("[AUTO] พบ Android project:")
        print(f" {project}")
        return project

    raise FileNotFoundError("ไม่พบ Android project หรือ ZIP")


# ============================================================
# PREPARE SOURCE
# ============================================================

def prepare_source(source):
    source = Path(source).resolve()

    if source.is_file():
        if source.suffix.lower() != ".zip":
            raise RuntimeError("ไฟล์ source ต้องเป็น Android project directory หรือ .zip")
        extracted = extract_zip(source)
        project = find_project_root(extracted)
        if not project:
            raise RuntimeError("แตก ZIP แล้ว แต่ไม่พบ Android Gradle project")
        return project

    if source.is_dir():
        if looks_like_android_project(source):
            return source
        project = find_project_root(source)
        if project:
            return project
        raise RuntimeError(f"ไม่พบ Android project ใน: {source}")

    raise RuntimeError(f"source ไม่ถูกต้อง: {source}")


# ============================================================
# ARGUMENTS
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Android APK Builder สำหรับ Termux/Android"
    )
    parser.add_argument("source", nargs="?", help="ZIP หรือโฟลเดอร์ Android project")
    parser.add_argument(
        "--no-install", action="store_true", help="Build APK อย่างเดียว ไม่ติดตั้ง"
    )
    parser.add_argument("--clean", action="store_true", help="Clean project ก่อน Build")
    parser.add_argument(
        "--release", action="store_true", help="พยายาม Build Release แทน Debug"
    )
    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main():
    args = parse_args()
    banner("ANDROID APK BUILDER")
    print(f"Script: {Path(__file__).resolve()}")
    print(f"Python: {sys.executable}")

    if not ensure_python():
        print("[FATAL] Python ไม่พร้อมใช้งาน")
        return 1

    ensure_basic_tools()
    show_storage_locations()

    try:
        source = resolve_source(args.source)
        print(f"\n[SOURCE] {source}")

        project = prepare_source(source)
        print(f"[PROJECT] {project}")

        label = (
            project.name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        )

        builder = Builder(
            project=project,
            label=label,
            no_install=args.no_install,
            clean=args.clean,
            release=args.release,
        )

        result = builder.build()
        if result:
            print("\n================================")
            print(" APK BUILD SUCCESS ")
            print("================================")
            print(f"\nAPK: {result}")
            return 0

        print("\n================================")
        print(" APK BUILD FAILED ")
        print("================================")
        return 1

    except KeyboardInterrupt:
        print("\n[STOP] ยกเลิกโดยผู้ใช้")
        return 130
    except Exception as error:
        print("\n================================")
        print(" FATAL ERROR ")
        print("================================")
        print(f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
