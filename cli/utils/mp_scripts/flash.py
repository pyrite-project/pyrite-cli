import sys,micropython
try:
    micropython.kbd_intr(-1)
    usb = sys.stdin.buffer
    sys.stdout.write('READY')
    f_size = FSIZE
    ack_every = ACK_EVERY
    ack_count = 0
    with open(FILE, 'wb') as f:
        while f_size:
            want = min(BFSIZE, f_size)
            d = b''
            while len(d) < want:
                c = usb.read(min(64, want - len(d)))
                if c:
                    d += c
            f.write(d)
            f_size -= len(d)
            ack_count += 1
            if ack_every and ack_count % ack_every == 0:
                f.flush()
                if f_size:
                    sys.stdout.write('+')
        f.flush()
    micropython.kbd_intr(3)
    rec = b''
    while not (b'ok' in rec):
        rec = (rec + (usb.read(1) or b''))[-16:]
VERIFY_CODE
    sys.stdout.write('ok')
except Exception as e:
    sys.stdout.write('FLASH_ERR:' + str(e) + '\n')
    raise
