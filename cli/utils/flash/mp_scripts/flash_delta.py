import os,sys,gc,micropython
try:
    import ubinascii
except Exception:
    ubinascii=None
try:
    path=FILE
    local_blocks=LOCAL_BLOCKS
    block_size=BLOCK_SIZE
    local_size=FSIZE
    action='full'
    offset=0
    truncate=0
    try:
        remote_size=os.stat(path)[6]
        truncate=1 if local_size<remote_size else 0
        if ubinascii is not None:
            mismatch=0
            i=0
            with open(path,'rb') as rf:
                for lcrc,lsize in local_blocks:
                    gc.collect()
                    data=rf.read(block_size)
                    if not data:
                        break
                    rcrc=ubinascii.crc32(data)&0xffffffff
                    if rcrc!=lcrc or len(data)!=lsize:
                        action='suffix'
                        offset=i*block_size
                        mismatch=1
                        break
                    i+=1
            if not mismatch:
                if local_size==remote_size:
                    action='skip'
                    offset=0
                    truncate=0
                elif local_size>remote_size:
                    action='append'
                    offset=remote_size
                    truncate=0
                else:
                    action='truncate'
                    offset=local_size
                    truncate=1
    except OSError:
        action='full'
        offset=0
        truncate=0
    except Exception:
        action='full'
        offset=0
        truncate=0
    remaining=local_size-offset
    if action=='full':
        remaining=local_size
        offset=0
        truncate=0
    elif action=='truncate':
        remaining=0
    sys.stdout.write('DELTA:%s:%d:%d:%d\n'%(action,offset,truncate,remaining))
    if action!='skip':
        micropython.kbd_intr(-1)
        usb=sys.stdin.buffer
        sys.stdout.write('READY')
        ack_every=ACK_EVERY
        ack_count=0
        tmp=path+'.pyrite_tmp'
        bak=path+'.pyrite_bak'
        f=open(path,'wb' if action=='full' else 'r+b')
        try:
            if action!='full':
                f.seek(offset)
            while remaining:
                want=min(BFSIZE,remaining)
                d=b''
                while len(d)<want:
                    c=usb.read(min(64,want-len(d)))
                    if c:
                        d+=c
                f.write(d)
                remaining-=len(d)
                ack_count+=1
                if ack_every and ack_count%ack_every==0:
                    f.flush()
                    if remaining:
                        sys.stdout.write('+')
            f.flush()
            if truncate:
                try:
                    f.truncate(local_size)
                    f.flush()
                except Exception:
                    f.close()
                    f=None
                    left=local_size
                    with open(path,'rb') as src:
                        with open(tmp,'wb') as dst:
                            while left:
                                data=src.read(min(BFSIZE,left))
                                if not data:
                                    break
                                dst.write(data)
                                left-=len(data)
                            dst.flush()
                    if left:
                        raise OSError('short copy')
                    try:
                        os.remove(bak)
                    except OSError:
                        pass
                    os.rename(path,bak)
                    try:
                        os.rename(tmp,path)
                    except Exception:
                        try:
                            os.rename(bak,path)
                        except Exception:
                            pass
                        raise
                    try:
                        os.remove(bak)
                    except OSError:
                        pass
        finally:
            if f:
                f.close()
            try:
                os.remove(tmp)
            except OSError:
                pass
        micropython.kbd_intr(3)
        rec=b''
        while not (b'ok' in rec):
            rec=(rec+(usb.read(1) or b''))[-16:]
VERIFY_CODE
    sys.stdout.write('ok')
except Exception as e:
    sys.stdout.write('FLASH_ERR:'+str(e)+'\n')
    raise
