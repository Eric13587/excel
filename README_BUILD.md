# How to Build LoanMaster for Windows

Since you are running this on Linux, you cannot directly compile a Windows executable (`.exe`) that is guaranteed to work. The best approach is to run the build process on a Windows machine.

Here are the steps to compile the application into a standalone portable `.exe` file on Windows.

## Prerequisites

1.  **Install Python**: Download and install Python 3.10 or newer from [python.org](https://www.python.org/downloads/).
    *   **Important**: Check the box **"Add Python to PATH"** during installation.

## Build Steps

1.  **Copy Files**: Copy the entire project folder (containing `loan.py`, `src` folder, `requirements.txt`, and `loan_master.spec`) to your Windows machine.

2.  **Open Command Prompt**:
    *   Press `Win + R`, type `cmd`, and press Enter.
    *   Navigate to the project folder:
        ```cmd
        cd path\to\your\project\folder
        ```

3.  **Install Dependencies**:
    Run the following command to install all required libraries and the builder:
    ```cmd
    pip install -r requirements.txt
    ```

4.  **Build the Executable**:
    Run PyInstaller using the provided spec file:
    ```cmd
    pyinstaller loan_master.spec
    ```

5.  **Locate the Application**:
    *   Once the process finishes (it may take a few minutes), go to the `dist` folder inside your project directory.
    *   You will find `LoanMaster.exe`.

## Usage
*   You can copy `LoanMaster.exe` to any other Windows computer.
*   It is a standalone file and does not require Python to be installed on the target machine.
*   When you run it, it will ask you to select or create a database file.
