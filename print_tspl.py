# print_tspl.py (run on Windows POS box)
import sys, win32print
PRINTER = "4BARCODE 4B-2034PA"  # change to your device name

def send_tspl(tspl_text, copies=1):
    hPrinter = win32print.OpenPrinter(PRINTER)
    hJob = win32print.StartDocPrinter(hPrinter, 1, ("Barcode Label", None, "RAW"))
    win32print.StartPagePrinter(hPrinter)
    for _ in range(copies):
        win32print.WritePrinter(hPrinter, tspl_text.encode('ascii', errors='ignore'))
    win32print.EndPagePrinter(hPrinter)
    win32print.EndDocPrinter(hPrinter)
    win32print.ClosePrinter(hPrinter)

if __name__ == "__main__":
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        tspl = f.read()
    copies = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    send_tspl(tspl, copies)
