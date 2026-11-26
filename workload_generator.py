#!/usr/bin/env python3
"""Gerador de carga sintética de CPU, RAM e disco (IOPS).

Todas as cargas rodam em threads do próprio processo, então todo o consumo
aparece no PID do gerador. Usa só a biblioteca padrão do Python.
"""

import argparse
import hashlib
import mmap
import os
import random
import select
import sys
import threading
import time

BLOCK_SIZE = 4096          # tamanho de cada operação de disco (4K)
CPU_SLICE = 0.01           # janela de controle da carga de CPU (10 ms)
CPU_CHUNK = 64 * 1024      # bloco processado por iteração do sha256
LAYOUT_CHUNK = 1024 * 1024 # bloco usado para criar o arquivo de teste
GIB = 1024 ** 3
SEED = 42                # semente fixa: execuções iguais fazem as mesmas operações de disco


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------
def cpu_worker(load, stop, result):
    """Ocupa um núcleo com `load`% de uso.

    A cada janela de 10 ms, calcula sha256 durante `load`% do tempo e dorme no
    restante. O sha256 roda em C e libera o GIL, por isso várias threads
    ocupam vários núcleos ao mesmo tempo.
    """
    data = os.urandom(CPU_CHUNK)
    busy = CPU_SLICE * load / 100
    ops = 0
    while not stop.is_set():
        start = time.perf_counter()
        while time.perf_counter() - start < busy:
            hashlib.sha256(data).digest()
            ops += 1
        rest = CPU_SLICE - (time.perf_counter() - start)
        if rest > 0:
            stop.wait(rest)
    result["ops"] = ops


# ---------------------------------------------------------------------------
# RAM
# ---------------------------------------------------------------------------
def ram_worker(gigabytes, stop, result):
    """Aloca `gigabytes` GB, escreve em todas as páginas e mantém até o fim."""
    size = int(gigabytes * GIB)
    buf = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    pattern = b"\xaa" * min(size, 64 * 1024 * 1024)
    offset = 0
    while offset < size and not stop.is_set():
        n = min(len(pattern), size - offset)
        buf[offset:offset + n] = pattern[:n]
        offset += n
    result["bytes"] = offset
    stop.wait()
    buf.close()


# ---------------------------------------------------------------------------
# Disco
# ---------------------------------------------------------------------------
def file_ready(path, size):
    return os.path.isfile(path) and os.path.getsize(path) == size


def write_file(path, size, parent_alive):
    """Escreve o arquivo de teste por inteiro, com dados reais.

    Em áreas nunca escritas, a leitura devolve zeros sem acessar o disco, por
    isso o arquivo é todo preenchido. A escrita vai para `<path>.<pid>.tmp`,
    que só é renomeado para `path` no fim: se `path` existe, ele está completo.
    O nome inclui o PID para que duas preparações simultâneas não se misturem.
    Se `parent_alive()` ficar falso (gerador encerrado), a escrita é abandonada.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    buf = mmap.mmap(-1, LAYOUT_CHUNK)  # memória alinhada, exigida pelo O_DIRECT
    buf.write(os.urandom(LAYOUT_CHUNK))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_DIRECT, 0o644)
    try:
        written = 0
        while written < size:
            if not parent_alive():
                raise RuntimeError("gerador encerrado")
            written += os.write(fd, buf)
        os.fsync(fd)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    finally:
        os.close(fd)
        buf.close()


def prepare_file(path, size):
    """Garante que o arquivo de teste existe, sem gastar recursos neste PID.

    Se o arquivo já existe com o tamanho certo, é reaproveitado e nada é feito.
    Senão, ele é criado por um processo neto (fork duplo), que é adotado pelo
    init. Assim a escrita do arquivo não aparece no PID do gerador, nem mesmo
    nos contadores de filhos (/proc/<PID>/io soma o I/O dos filhos que o
    processo espera, mas não o dos netos).
    """
    if file_ready(path, size):
        print(f"[IOPS] Reaproveitando arquivo de teste {path}.")
        return
    print(f"[IOPS] Preparando arquivo de teste {path} ({size / GIB:g} GB) "
          "em outro processo...", flush=True)
    r, w = os.pipe()
    child = os.fork()
    if child == 0:  # filho: cria o neto e sai na hora
        os.close(r)
        if os.fork() == 0:  # neto: escreve o arquivo
            error = b""
            # Se o gerador morrer, a ponta de leitura do pipe fecha e o poll
            # da ponta de escrita acusa erro: o neto para em vez de continuar
            # gastando disco em segundo plano.
            watch = select.poll()
            watch.register(w, select.POLLERR)
            try:
                with open("/proc/self/comm", "w") as f:
                    f.write("wlg-prepare")
                write_file(path, size, lambda: not watch.poll(0))
            except BaseException as e:  # inclui Ctrl+C
                error = f"{type(e).__name__}: {e}".encode() or b"erro"
            try:
                os.write(w, error)
            except OSError:
                pass  # gerador já saiu; ninguém para avisar
            finally:
                os._exit(1 if error else 0)  # nunca volta ao código do gerador
        os._exit(0)
    os.close(w)
    os.waitpid(child, 0)
    try:
        chunks = []
        while True:  # termina quando o neto sai e fecha o pipe
            data = os.read(r, 4096)
            if not data:
                break
            chunks.append(data)
    except KeyboardInterrupt:
        sys.exit("\n[IOPS] Preparação interrompida.")
    finally:
        os.close(r)
    if not file_ready(path, size):
        msg = b"".join(chunks).decode(errors="replace") or "motivo desconhecido"
        sys.exit(f"[IOPS] Falha ao preparar {path}: {msg}")
    print("[IOPS] Arquivo de teste pronto.")


def io_worker(path, size, iops, readwrite, stop, result):
    """Faz leituras e escritas aleatórias de 4K, sem cache, a `iops` operações/s.

    Cada operação é leitura com probabilidade `readwrite`% e escrita no
    restante. As operações são agendadas em intervalos fixos de 1/iops
    segundos; se o disco não acompanhar, a taxa fica no máximo possível.
    A semente fixa `SEED` define a sequência de posições e de leituras/escritas,
    então toda execução faz exatamente as mesmas operações.
    """
    fd = os.open(path, os.O_RDWR | os.O_DIRECT)
    buf = mmap.mmap(-1, BLOCK_SIZE)  # memória alinhada, exigida pelo O_DIRECT
    buf.write(os.urandom(BLOCK_SIZE))
    rng = random.Random(SEED)
    blocks = size // BLOCK_SIZE
    interval = 1.0 / iops
    reads = writes = 0
    next_op = time.perf_counter()
    try:
        while not stop.is_set():
            delay = next_op - time.perf_counter()
            if delay > 0:
                stop.wait(delay)
                if stop.is_set():
                    break
            elif delay < -1.0:
                # Atrasado demais (disco lento): não acumula rajada.
                next_op = time.perf_counter()
            offset = rng.randrange(blocks) * BLOCK_SIZE
            if rng.random() * 100 < readwrite:
                os.preadv(fd, [buf], offset)
                reads += 1
            else:
                os.pwritev(fd, [buf], offset)
                writes += 1
            next_op += interval
    finally:
        os.close(fd)
        buf.close()
    result["reads"] = reads
    result["writes"] = writes


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------
def percentage(value):
    n = int(value)
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError("deve estar entre 0 e 100")
    return n


def positive(cast):
    def parse(value):
        n = cast(value)
        if n <= 0:
            raise argparse.ArgumentTypeError("deve ser maior que zero")
        return n
    return parse


def parse_args():
    p = argparse.ArgumentParser(
        description="Gerador de carga sintética (CPU, RAM e IOPS) em threads de um único processo.")
    p.add_argument("-c", "--cores", type=positive(int), default=1,
                   help="núcleos usados pela carga de CPU (padrão: 1)")
    p.add_argument("-u", "--cpu", type=percentage,
                   help="percentual de uso em cada núcleo (0-100)")
    p.add_argument("-r", "--ram", type=positive(float),
                   help="memória a alocar, em GB")
    p.add_argument("-i", "--iops", type=positive(int),
                   help="operações de disco por segundo")
    p.add_argument("-w", "--readwrite", type=percentage, default=50,
                   help="percentual de leituras; o restante é escrita (padrão: 50)")
    p.add_argument("-t", "--time", type=positive(float), default=60,
                   help="duração da carga, em segundos (padrão: 60)")
    p.add_argument("--io-file", default="teste_io.dat",
                   help="arquivo usado pela carga de disco (padrão: teste_io.dat)")
    p.add_argument("--io-size", type=positive(float), default=4,
                   help="tamanho do arquivo de disco, em GB (padrão: 4)")
    args = p.parse_args()
    if args.cpu is None and args.ram is None and args.iops is None:
        p.error("informe ao menos uma carga: --cpu, --ram ou --iops")
    return args


def main():
    args = parse_args()
    print(f"[Gerador] PID: {os.getpid()}")

    stop = threading.Event()
    threads = []  # (nome, thread, resultado)

    def start(name, target, *target_args):
        result = {}
        t = threading.Thread(target=target, args=(*target_args, stop, result), name=name)
        threads.append((name, t, result))

    if args.cpu is not None:
        for n in range(args.cores):
            start(f"cpu-{n}", cpu_worker, args.cpu)

    if args.ram is not None:
        start("ram", ram_worker, args.ram)

    if args.iops is not None:
        io_size = int(args.io_size * GIB) // BLOCK_SIZE * BLOCK_SIZE
        prepare_file(args.io_file, io_size)
        start("iops", io_worker, args.io_file, io_size, args.iops, args.readwrite)

    began = time.perf_counter()
    for _, t, _ in threads:
        t.start()
    print(f"[Gerador] {len(threads)} thread(s) de carga rodando por {args.time:g} s. "
          "Ctrl+C encerra antes.")

    try:
        stop.wait(args.time)
    except KeyboardInterrupt:
        print("\n[Gerador] Interrompido.")
    stop.set()
    for _, t, _ in threads:
        t.join()
    elapsed = time.perf_counter() - began

    print(f"[Gerador] Concluído em {elapsed:.1f} s.")
    cpu_ops = sum(r.get("ops", 0) for name, _, r in threads if name.startswith("cpu-"))
    for name, _, r in threads:
        if name == "ram":
            print(f"[RAM]  {r.get('bytes', 0) / GIB:g} GB alocados")
        elif name == "iops":
            reads, writes = r.get("reads", 0), r.get("writes", 0)
            print(f"[IOPS] {(reads + writes) / elapsed:.0f} IOPS obtidos "
                  f"({reads / elapsed:.0f} leitura, {writes / elapsed:.0f} escrita)")
    if args.cpu is not None:
        print(f"[CPU]  {cpu_ops / elapsed:.0f} blocos sha256/s em {args.cores} núcleo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
