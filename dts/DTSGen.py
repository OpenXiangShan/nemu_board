#!/usr/bin/env python3

from functools import cmp_to_key
import re

class DTSGen:
    def __init__(self,
                 compatible: str = "xiangshan,nemu-board",
                 model: str = "XiangShan",
                 isa_extensions: list = ["i", "m", "a", "f", "d", "c"],
                 mmu_type="riscv,sv39",
                 timebase_freq: int = 10000000, # 10 MHz
                 nr_harts: int = 1,
                 clint_addr: int = 0x38000000,
                 plic_addr: int = 0x3c000000,
                 memories: list = [(0x80000000, 8*1024*1024*1024)], # [(start, size)], default 8 GB at 2 GB
                 reserved_memories: list = [], # [(start, size)]
                 bootargs: str = "console=hvc0 earlycon=sbi rdinit=/sbin/init",
                 rng_seed: bytes = b"NEMU_BOARD",
                 uartlite_addr: int = 0x40600000,
                 nemu_sdhci_addr: int = None):
        self.isa_extensions = isa_extensions
        self.mmu_type = mmu_type
        self.timebase_freq = timebase_freq
        self.nr_harts = nr_harts
        self.clint_addr = clint_addr
        self.plic_addr = plic_addr
        self.memories = memories
        self.reserved_memories = reserved_memories
        self.compatible = compatible
        self.model = model
        self.bootargs = bootargs
        # Note: console=hvc0 is a hack for NEMU Uartlite to use SBI
        # console to get around the lack of TX FIFO empty interrupt
        # implementation. The proper param should be "console=ttyUL0"
        # for Uartlite IP on Xilinx FPGA.
        # Ensure we have set the following kernel configs:
        # CONFIG_NONPORTABLE=y
        # CONFIG_HVC_RISCV_SBI=y
        self.rng_seed = rng_seed
        self.soc_devices = [] # [dev_str0, ...]
        if uartlite_addr is not None and plic_addr is not None:
            self.soc_devices.append(self.__gen_uartlite(uartlite_addr, 3))
        if nemu_sdhci_addr is not None:
            self.soc_devices.append(self.__gen_nemu_sdhci(nemu_sdhci_addr))

    def indent(text, level: int = 4):
        indent_str = ' ' * level
        return "\n".join(indent_str + line if line.strip() != "" else line for line in text.split("\n"))
    
    def gen_addrsize(addrsize, cell_number):
        res = ""
        for i in range(cell_number):
            res += f"0x{(addrsize >> (32 * (cell_number - i - 1))) & 0xFFFFFFFF:x} "
        return res.strip()
    
    def __gen_memory(self, memory_start, memory_size):
        return f"""
memory@{memory_start:x} {{
    device_type = "memory";
    reg = <{DTSGen.gen_addrsize(memory_start, 2)} {DTSGen.gen_addrsize(memory_size, 2)}>;
}};
""".strip()

    def __gen_isa_string(self, isa, cacheline_size=64):
        # Cache parameters are used for Zicbom / Zicbop / Zicboz
        # support in Linux
        return f"""
{ f"riscv,cbom-block-size = <{cacheline_size}>;" if "zicbom" in isa else "" }
{ f"riscv,cbop-block-size = <{cacheline_size}>;" if "zicbop" in isa else "" }
{ f"riscv,cboz-block-size = <{cacheline_size}>;" if "zicboz" in isa else "" }
riscv,isa = "rv64i{'m' if 'm' in isa else ''}{'a' if 'a' in isa else ''}{'f' if 'f' in isa else ''}{'d' if 'd' in isa else ''}{'c' if 'c' in isa else ''}{'v' if 'v' in isa else ''}{'h' if 'h' in isa else ''}";
riscv,isa-base = "rv64i";
riscv,isa-extensions = {", ".join(f'"{ext}"' for ext in isa)};
""".strip()

    def __gen_cpu_node(self, hart_id, isa):
        # Reference: https://github.com/torvalds/linux/blob/master/Documentation/devicetree/bindings/riscv/cpus.yaml
        return f"""
cpu{hart_id}: cpu@{hart_id} {{
    compatible = "riscv";
    device_type = "cpu";
    mmu-type = "{self.mmu_type}";
    reg = <{hart_id}>;
{DTSGen.indent(self.__gen_isa_string(isa))}

    cpu{hart_id}_intc: interrupt-controller {{
        #interrupt-cells = <1>;
        compatible = "riscv,cpu-intc";
        interrupt-controller;
    }};
}};
""".strip()

    def __gen_cpus(self):
        return f"""
cpus {{
    #address-cells = <1>;
    #size-cells = <0>;
    timebase-frequency = <{self.timebase_freq}>;

{DTSGen.indent('\n'.join(self.__gen_cpu_node(hart_id, self.isa_extensions) + "\n" for hart_id in range(self.nr_harts)))}
}};
""".strip()

    def __gen_clint(self):
        # Reference: https://github.com/torvalds/linux/blob/master/Documentation/devicetree/bindings/timer/sifive%2Cclint.yaml
        if self.clint_addr == None:
            return ""
        # 3 is MSI, 7 is MTI
        cpu_intc_list = [f"<&cpu{hart_id}_intc 3>, <&cpu{hart_id}_intc 7>" for hart_id in range(self.nr_harts)]
        cpu_intc_head = "    interrupts-extended = ";
        cpu_intc_str = cpu_intc_head
        for each_intc in zip(cpu_intc_list, list(range(self.nr_harts))):
            cpu_intc_str += each_intc[0]
            if each_intc[1] != self.nr_harts - 1:
                cpu_intc_str += ",\n" + " " * (len(cpu_intc_head))
        return f"""
clint: clint@{self.clint_addr:x} {{
    compatible = "riscv,clint0";
    reg = <{DTSGen.gen_addrsize(self.clint_addr, 2)} {DTSGen.gen_addrsize(0x10000, 2)}>;
{cpu_intc_str};
}};
""".strip()

    def __gen_plic(self, max_priority: int = 7, ndev: int = 64):
        # Reference: https://github.com/torvalds/linux/blob/master/Documentation/devicetree/bindings/interrupt-controller/sifive%2Cplic-1.0.0.yaml
        if self.plic_addr is None:
            return ""
        # 11 is MEI, 9 is SEI
        cpu_intc_list = [f"<&cpu{hart_id}_intc 11>, <&cpu{hart_id}_intc 9>" for hart_id in range(self.nr_harts)]
        cpu_intc_head = "    interrupts-extended = ";
        cpu_intc_str = cpu_intc_head
        for each_intc in zip(cpu_intc_list, list(range(self.nr_harts))):
            cpu_intc_str += each_intc[0]
            if each_intc[1] != self.nr_harts - 1:
                cpu_intc_str += ",\n" + " " * (len(cpu_intc_head))
        return f"""
plic: plic@{self.plic_addr:x} {{
    compatible = "riscv,plic0";
    reg = <{DTSGen.gen_addrsize(self.plic_addr, 2)} {DTSGen.gen_addrsize(0x4000000, 2)}>;
    #interrupt-cells = <1>;
    interrupt-controller;
{cpu_intc_str};
    riscv,max-priority = <{max_priority}>;
    riscv,ndev = <{ndev}>;
}};
""".strip()

    def __gen_reserved_memory(self):
        if len(self.reserved_memories) == 0:
            return ""
        res = f"""
reserved-memory {{
    #address-cells = <2>;
    #size-cells = <2>;
    ranges;
"""
        for ((start, size), idx) in zip(self.reserved_memories, list(range(len(self.reserved_memories)))):
            res += DTSGen.indent(f"""
resv{idx}@{start:x} {{
    reg = <{DTSGen.gen_addrsize(start, 2)} {DTSGen.gen_addrsize(size, 2)}>;
    no-map;
}};
""".strip() + "\n")
        res += "};"
        return res
    
    def __gen_uartlite(self, uartlite_addr, plic_addr):
        # https://github.com/torvalds/linux/blob/master/Documentation/devicetree/bindings/serial/xlnx%2Copb-uartlite.yaml
        # Uartlite hardware does not handle clock division, so we do
        # not specify clocks here
        return f"""
serial@{uartlite_addr:x} {{
    compatible = "xlnx,xps-uartlite-1.00.a";
    reg = <{DTSGen.gen_addrsize(uartlite_addr, 2)} {DTSGen.gen_addrsize(0x1000, 2)}>;
    interrupts-extended = <&plic {plic_addr}>;
    current-speed = <115200>;
    xlnx,data-bits = <8>;
    xlnx,use-parity = <0>;
}};
""".strip()

    def __gen_nemu_sdhci(self, sd_addr):
        return f"""
mmc@{sd_addr:x} {{
    compatible = "nemu,sdhost";
    reg = <{DTSGen.gen_addrsize(sd_addr, 2)} {DTSGen.gen_addrsize(0x1000, 2)}>;
}};
""".strip()
    
    def __gen_soc(self):
        res = f"""
soc {{
    #address-cells = <2>;
    #size-cells = <2>;
    compatible = "simple-bus";
    ranges;
""" + "\n"
        for dev_str in self.soc_devices:
            res += DTSGen.indent(dev_str, 4) + "\n\n"
        res += "};"
        return res.strip()
    
    def sort_isa_extensions(ext_list: list):
        SINGLE_ORDER = list("IMAFDQLCBKJTPVH")
        # Helper to categorize an extension name
        def ext_category(ext: str):
            """Return (category, key1, key2) so sorting works:
            category:
            0 = single-letter standard
            1 = standard Z* (multi-letter beginning with 'Z')
            2 = supervisor-level S*, hypervisor H*, machine-level Zxm*, etc.
            3 = non-standard X*
            key1/key2: for ordering within category
            """
            # normalize
            e = ext.strip()
            # Remove version suffixes (e.g., p0, p1p2, digits) for ordering only
            e_base = re.sub(r'[0-9pP].*$', '', e)
            # Single-letter standard?
            if len(e_base) == 1 and e_base.upper() in SINGLE_ORDER:
                return (0, SINGLE_ORDER.index(e_base.upper()), "")
            # Supervisor-level (S*), Hypervisor-level (H*), Machine-level (Zxm*), etc.
            if re.match(r'^(S|Sh|Sm|Zxm)', e_base, re.IGNORECASE):
                return (2, e_base.lower(), "")
            # Z* standard unprivileged
            if e_base.startswith("Z"):
                # use the letter after Z to map to SINGLE_ORDER index if possible
                cat0 = e_base[1].upper()
                idx = SINGLE_ORDER.index(cat0) if cat0 in SINGLE_ORDER else ord(cat0)
                return (1, idx, e_base.lower())
            # Non-standard X*
            if e_base.startswith("X"):
                return (3, e_base.lower(), "")
            # Unknown / fallback
            return (4, e_base.lower(), "")
        def compare_ext(a: str, b: str):
            ca = ext_category(a)
            cb = ext_category(b)
            if ca < cb:
                return -1
            if ca > cb:
                return 1
            # same category — compare category-specific keys
            if ca[1] != cb[1]:
                return -1 if ca[1] < cb[1] else 1
            # tie-breaker: lex order on base
            return -1 if ca[2] < cb[2] else (1 if ca[2] > cb[2] else 0)
        return sorted(ext_list, key=cmp_to_key(compare_ext))
            
    
    def get_isa_extensions_by_rva_profile(rva_profile: str):
        RVA20U64 = {
            "i", "m", "a", "f", "d", "c",
            "zicsr", "zicntr"
            # "ziccif", "ziccrse", "ziccamoa", "za128rs", "zicclsm"
        }
        RVA20S64 = RVA20U64.union({
            "zifencei"
            # "ss1p11", "svbare", "sv39", "svade", "ssccptr",
            # "sstvecd", "sstvala"
        })
        # Don't use 'b' for backward compatibility as it ratified in 2024
        RVA22U64 = RVA20U64.union({
            "zba", "zbb", "zbs", "zihpm", "zihintpause", "zicbom",
            "zicbop", "zicboz", "zfhmin", "zkt"
            # "za64rs", "zic64b"
        })
        RVA22S64 = RVA22U64.union({
            "zifencei", "svpbmt", "svinval"
            # "ss1p12", "svbare", "sv39", "svade", "ssccptr",
            # "sstvecd", "sstvala", "sscounterenw"
        })
        RVA23U64 = RVA22U64.union({
            "v", "zvfhmin", "zvbb", "zvkt", "zihintntl", "zicond",
            "zimop", "zcmop", "zcb", "zfa", "zawrs", "supm"
        })
        RVA23S64 = RVA23U64.union({
            "zifencei", "svpbmt", "svinval", "sstc", "sscofpmf",
            "sha", "h"
            # "ss1p13", "ssnpm", "ssu64xl", "ssstateen", "shtvala",
            # "shvstvecd", "shvsatpa", "shgatpa"
        })
        if rva_profile == "rva20u64":
            return DTSGen.sort_isa_extensions(list(RVA20U64))
        elif rva_profile == "rva20s64":
            return DTSGen.sort_isa_extensions(list(RVA20S64))
        elif rva_profile == "rva22u64":
            return DTSGen.sort_isa_extensions(list(RVA22U64))
        elif rva_profile == "rva22s64":
            return DTSGen.sort_isa_extensions(list(RVA22S64))
        elif rva_profile == "rva23u64":
            return DTSGen.sort_isa_extensions(list(RVA23U64))
        elif rva_profile == "rva23s64":
            return DTSGen.sort_isa_extensions(list(RVA23S64))
        assert False, f"Unknown rva profile string: {rva_profile}"
    
    def add_device(self, dev_str: str):
        self.soc_devices.append(dev_str)
        return self

    def gen_dts(self):
        return f"""
/dts-v1/;

/ {{
    #address-cells = <2>;
    #size-cells = <2>;
    compatible = "{self.compatible}";
    model = "{self.model}";
    
    chosen {{
{DTSGen.indent(f'bootargs = "{self.bootargs}";', 8) if self.bootargs else ''}
{DTSGen.indent(f'rng-seed = /bits/ 8 <{" ".join(f"0x{b:02x}" for b in self.rng_seed)}>;', 8) if self.rng_seed else ''}
    }};

{DTSGen.indent('\n'.join(self.__gen_memory(start, size) for (start, size) in self.memories))}

{DTSGen.indent(self.__gen_cpus())}

{DTSGen.indent(self.__gen_clint())}

{DTSGen.indent(self.__gen_plic())}

{DTSGen.indent(self.__gen_reserved_memory())}

{DTSGen.indent(self.__gen_soc())}

}};
""".strip()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser("DTSGen: Generate Device Tree Source for NEMU Board")
    parser.add_argument("--nr-harts", "-n", type=int, default=1, help="Number of harts")
    parser.add_argument("--nemu-sdhci-addr", "-s", type=lambda x: int(x,0), default=None, help="NEMU SDHCI address")
    parser.add_argument("--reserve-mem", "-r", type=lambda x: int(x,0), nargs=2, action='append', default=[], help="Reserved memory regions, specify as start size pairs in hex")
    parser.add_argument("--isa-extensions", "-i", type=str, nargs='+', default=["i", "m", "a", "f", "d", "c"], help="ISA extensions, e.g., i m a f d c")
    parser.add_argument("--rva-profile", "-p", type=str, default="rva20u64", help="rva profile string, e.g., rva20u64, rva22s64, rva23s64")
    parser.add_argument("--bootargs", "-b", type=str, default="console=hvc0 earlycon=sbi rdinit=/sbin/init", help="Kernel boot arguments")
    parser.add_argument("--memory-size", "-m", type=int, default=8*1024*1024*1024, help="Total memory size in bytes")
    parser.add_argument("--mmu-type", type=str, default="riscv,sv39", help="MMU type")
    parser.add_argument("--timebase-freq", "-t", type=int, default=10000000, help="Timebase frequency in Hz (default 10 MHz)")
    args = parser.parse_args()
    isa_exts = set(args.isa_extensions)
    if args.rva_profile:
        isa_exts.update(set(DTSGen.get_isa_extensions_by_rva_profile(args.rva_profile)))
    dtsgen = DTSGen(
        nr_harts=args.nr_harts,
        nemu_sdhci_addr=args.nemu_sdhci_addr,
        reserved_memories=args.reserve_mem,
        isa_extensions=DTSGen.sort_isa_extensions(list(isa_exts)),
        bootargs=args.bootargs,
        mmu_type=args.mmu_type,
        memories=[(0x80000000, args.memory_size)]
    )
    print(dtsgen.gen_dts())
