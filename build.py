import os
import subprocess
import sys
from PIL import Image

def build():
    print("Initializing LoanMaster Build Sequence (Target: Windows)...")
    
    # 1. Prepare Icon
    print("Preparing Icon...")
    icon_png = "resources/icon.png"
    icon_ico = "resources/icon.ico"
    
    if os.path.exists(icon_png):
        try:
            img = Image.open(icon_png)
            img.save(icon_ico, format='ICO', sizes=[(256, 256)])
            print(f"Converted {icon_png} to {icon_ico}")
        except Exception as e:
            print(f"Warning: Could not convert icon: {e}")
            icon_ico = None
    else:
        print("Warning: icon.png not found in resources/")
        icon_ico = None

    # 2. Define Nuitka Command
    # Optimal Options for PyQt6 on Windows
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",                    # Create a portable folder (fastest, most stable)
        "--enable-plugin=pyqt6",           # Essential for PyQt6
        "--windows-console-mode=disable",       # GUI only, no terminal window
        "--lto=yes",                       # Link Time Optimization (smaller, faster binary)
        "--deployment",                    # Disable Nuitka warnings intended for dev
        "--show-progress",                 # Visual feedback
        "--output-dir=build",              # Output directory
        "--output-filename=LoanMaster",    # Name of the executable
        "--include-data-dir=resources=resources", # Include resources folder
        "loan.py"                          # Entry Point
    ]
    
    if icon_ico and os.path.exists(icon_ico):
        cmd.append(f"--windows-icon-from-ico={icon_ico}")

    print("\nExecuting Nuitka Build Command:")
    print(" ".join(cmd))
    print("\nThis process may take several minutes...")
    
    # 3. Execution
    try:
        # Note: On Linux this compiles for Linux. On Windows for Windows.
        # User requested script to run on Windows.
        if os.name == 'nt':
            subprocess.check_call(cmd)
            print("\nBUILD SUCCESSFUL!")
            print(f"Artifacts located in: {os.path.abspath('build/loan.dist')}")
        else:
            print("\n[INFO] You are running on Linux.")
            print("To build for Windows, please transfer this project to a Windows machine")
            print("and run: python build.py")
            print("(Ensure 'pip install nuitka pillow' is run first)")
            
            # Optional: Dry run or Linux build?
            # subprocess.check_call(cmd) # Uncomment to build for Linux now
            
    except subprocess.CalledProcessError as e:
        print(f"\nBUILD FAILED with Code {e.returncode}")
        sys.exit(1)

if __name__ == "__main__":
    build()
