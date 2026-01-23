#!/usr/bin/env python3
# raw_impulse_sniff.py - minimal raw sniffer for IMPULSE_TEST frames
import time
import serial

PORT = "/dev/ttyUSB0"
BAUD = 115200

SYNC = 0xA5
TYPEVER_IMPULSE = 0x61  # type=0x6, ver=0x1

def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    buf = bytearray()
    t0 = time.time()
    print("sniffing... Ctrl+C to stop")
    try:
        while True:
            buf.extend(ser.read(512))
            while True:
                if len(buf) < 4:
                    break
                idx = buf.find(bytes([SYNC]))
                if idx < 0:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]
                if len(buf) < 4:
                    break
                typever = buf[1]
                plen = buf[2]
                need = 1 + 1 + 1 + plen + 2
                if len(buf) < need:
                    break
                frame = bytes(buf[:need])
                del buf[:need]
                if typever == TYPEVER_IMPULSE:
                    dt = time.time() - t0
                    print(f"[IMPULSE RAW] len={plen} t={dt:.2f}s head={frame[:6].hex()}")
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()

if __name__ == "__main__":
    main()
