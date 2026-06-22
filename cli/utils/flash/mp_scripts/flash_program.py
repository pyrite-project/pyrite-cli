import sys, micropython

try:
    micropython.kbd_intr(-1)
    usb = sys.stdin.buffer
    sys.stdout.write('READY')

    entries = FILES
    ack_every = ACK_EVERY
    ack_count = 0
    total_left = sum(file_size for file_size, file_path in entries)
    for file_size, file_path in entries:
        with open(file_path, 'wb') as f:
            remaining = file_size
            while remaining:
                want = min(remaining, BFSIZE)
                d = b''
                while len(d) < want:
                    c = usb.read(min(64, want - len(d)))
                    if c:
                        d += c
                f.write(d)
                remaining -= len(d)
                total_left -= len(d)
                ack_count += 1
                if ack_every and ack_count % ack_every == 0:
                    f.flush()
                    if total_left:
                        sys.stdout.write('+')
            f.flush()
VERIFY_CODE
    micropython.kbd_intr(3)
except Exception as e:
    sys.stdout.write('FLASH_ERR:' + str(e) + '\n')
    raise
