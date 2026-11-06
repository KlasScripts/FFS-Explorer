"""Print the native call stacks in a crash.dmp written by app/startup_log.py.

usage: python scripts/read_crash_dump.py <crash.dmp> [--all]

Standard library only, no debugger needed. For each thread it scans the
saved stack for return addresses: values that point into a loaded module
just after a call instruction (checked against the module file on disk,
so run it on the machine that crashed, or with the same build). Each is
named by the nearest exported function. That is a heuristic, not a real
unwind: it can show a stale frame or miss one, and a private function is
labelled with the export before it. It is enough to see which DLL and
which Qt / C++ runtime function called abort().

The thread that aborted is the one that wrote the dump (its stack holds
dbghelp.dll or dbgcore.dll); only it is printed unless --all is given. Read it from the
top: dbghelp/dbgcore and ctypes/libffi (the dump handler), then ucrtbase (abort
and raise), then the code that called abort(). On Windows on ARM the
system DLLs are ARM64X, so their names here are rough; the app's own DLLs
(Qt, Python, PySide) name well.
"""
import bisect
import os
import struct
import sys


def _streams(buf):
    sig, _ver, n, rva = struct.unpack_from('<4sIII', buf, 0)
    if sig != b'MDMP':
        raise SystemExit('not a minidump')
    out = {}
    for i in range(n):
        typ, size, srva = struct.unpack_from('<III', buf, rva + 12 * i)
        out[typ] = (srva, size)
    return out


def _mdstring(buf, rva):
    (n,) = struct.unpack_from('<I', buf, rva)
    return buf[rva + 4: rva + 4 + n].decode('utf-16-le', 'replace')


def _modules(buf, streams):
    rva, _ = streams[4]
    (n,) = struct.unpack_from('<I', buf, rva)
    mods = []
    for i in range(n):
        base, size, _ck, _ts, name_rva = struct.unpack_from('<QIIII', buf, rva + 4 + 108 * i)
        mods.append((base, size, _mdstring(buf, name_rva)))
    return sorted(mods)


def _threads(buf, streams):
    rva, _ = streams[3]
    (n,) = struct.unpack_from('<I', buf, rva)
    out = []
    for i in range(n):
        o = rva + 4 + 48 * i
        tid, = struct.unpack_from('<I', buf, o)
        start, msize, mrva = struct.unpack_from('<QII', buf, o + 24)
        ctx_size, ctx_rva = struct.unpack_from('<II', buf, o + 40)
        rsp = struct.unpack_from('<Q', buf, ctx_rva + 0x98)[0] if ctx_size >= 0xA0 else start
        rip = struct.unpack_from('<Q', buf, ctx_rva + 0xF8)[0] if ctx_size >= 0x100 else 0
        out.append((tid, start, buf[mrva: mrva + msize], rsp, rip))
    return out


class _Pe:
    """Exports and code bytes of one module file, read lazily."""
    def __init__(self, path):
        self.ok = False
        self.exports = []
        try:
            with open(path, 'rb') as f:
                self.data = f.read()
        except OSError:
            return
        d = self.data
        pe = struct.unpack_from('<I', d, 0x3C)[0]
        nsec = struct.unpack_from('<H', d, pe + 6)[0]
        opt = pe + 24
        magic = struct.unpack_from('<H', d, opt)[0]
        dd = opt + (112 if magic == 0x20B else 96)
        sec = opt + struct.unpack_from('<H', d, pe + 20)[0]
        self.sections = []
        for i in range(nsec):
            vsize, va, rsize, raw = struct.unpack_from('<IIII', d, sec + 40 * i + 8)
            self.sections.append((va, max(vsize, rsize), raw))
        exp_rva, _exp_size = struct.unpack_from('<II', d, dd)
        self.ok = True
        if not exp_rva:
            return
        e = self._off(exp_rva)
        nfun, nnames, afun, aname, aord = struct.unpack_from('<IIIII', d, e + 20)
        funcs = [struct.unpack_from('<I', d, self._off(afun) + 4 * i)[0] for i in range(nfun)]
        for i in range(nnames):
            nrva = struct.unpack_from('<I', d, self._off(aname) + 4 * i)[0]
            ordi = struct.unpack_from('<H', d, self._off(aord) + 2 * i)[0]
            no = self._off(nrva)
            name = d[no: d.index(b'\0', no)].decode('ascii', 'replace')
            self.exports.append((funcs[ordi], name))
        self.exports.sort()
        self._keys = [a for a, _ in self.exports]

    def _off(self, rva):
        for va, size, raw in self.sections:
            if va <= rva < va + size:
                return raw + rva - va
        return None

    def after_call(self, rva):
        o = self._off(rva)
        if o is None or o < 7:
            return False
        b = self.data[o - 7: o]
        # ARM64 (an x64 app on Windows on ARM loads ARM64X system DLLs):
        # BL imm26 or BLR Xn just before the return address.
        w = struct.unpack_from('<I', b, 3)[0]
        if (w & 0xFC000000) == 0x94000000 or (w & 0xFFFFFC1F) == 0xD63F0000:
            return True
        return (b[2] == 0xE8 or b[1] == 0xFF and b[2] in (0x15, 0x55, 0x95) or
                b[5] == 0xFF and (b[6] & 0xF8) == 0xD0 or b[4] == 0xFF and (b[5] & 0xF8) in (0x50, 0x10))

    def name(self, rva):
        if not self.exports:
            return f'+{rva:#x}'
        i = bisect.bisect_right(self._keys, rva) - 1
        if i < 0:
            return f'+{rva:#x}'
        a, n = self.exports[i]
        return f'{n}+{rva - a:#x}'


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        raise SystemExit(__doc__)
    buf = open(args[0], 'rb').read()
    streams = _streams(buf)
    mods = _modules(buf, streams)
    bases = [m[0] for m in mods]
    pes = {}

    def resolve(addr):
        i = bisect.bisect_right(bases, addr) - 1
        if i < 0:
            return None
        base, size, path = mods[i]
        if not base <= addr < base + size:
            return None
        pe = pes.get(path)
        if pe is None:
            pe = pes[path] = _Pe(path)
        if not pe.ok or not pe.after_call(addr - base):
            return None
        return f'{os.path.basename(path)}!{pe.name(addr - base)}'

    stacks = []
    for tid, start, mem, rsp, _rip in _threads(buf, streams):
        frames = []
        first = max(0, rsp - start)
        for off in range(first - first % 8, len(mem) - 7, 8):
            label = resolve(struct.unpack_from('<Q', mem, off)[0])
            if label:
                frames.append(label)
        stacks.append((tid, frames))
    # The aborting thread is the one that ran startup_log's handler, which
    # called MiniDumpWriteDump (dbghelp.dll, which hands the work to dbgcore.dll).
    aborted = ([s for s in stacks if any(f.lower().startswith(('dbghelp.dll!', 'dbgcore.dll!')) for f in s[1])]
               or [s for s in stacks if any(f.lower().startswith('ucrtbase.dll!') for f in s[1])][:1])
    show = stacks if '--all' in sys.argv else (aborted or stacks)
    for tid, frames in show:
        mark = '  <- called abort()' if (tid, frames) in aborted else ''
        print(f'Thread {tid:#x}{mark}  ({len(frames)} likely return addresses, innermost first)')
        for f in frames[:60]:
            print('   ', f)
        print()


if __name__ == '__main__':
    main()
