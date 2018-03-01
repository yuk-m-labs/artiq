use board::{csr, clock};
use core::slice;
use byteorder::{ByteOrder, BigEndian};

const CCLK_BIT: u8 = 1 << 0;
const DIN_BIT: u8 = 1 << 1;
const DONE_BIT: u8 = 1 << 2;
const INIT_B_BIT: u8 = 1 << 3;
const PROGRAM_B_BIT: u8 = 1 << 4;

const GATEWARE: *mut u8 = csr::CONFIG_SLAVE_FPGA_GATEWARE as *mut u8;

unsafe fn shift_u8(data: u8) {
    for i in 0..8 {
        let mut bits: u8 = PROGRAM_B_BIT;
        if data & (0x80 >> i) != 0 {
            bits |= DIN_BIT;
        }
        // Without delays, this is about 6 MHz CCLK which is fine.
        csr::slave_fpga_cfg::out_write(bits);
        // clock::spin_us(1);
        csr::slave_fpga_cfg::out_write(bits | CCLK_BIT);
        // clock::spin_us(1);
    }
}

pub fn load() -> Result<(), &'static str> {
    info!("Loading slave FPGA gateware...");

    let header = unsafe { slice::from_raw_parts(GATEWARE, 8) };
    let magic = BigEndian::read_u32(&header[0..]);
    let length = BigEndian::read_u32(&header[4..]) as usize;

    if magic != 0x53415231 {  // "SAR1"
        return Err("Slave FPGA gateware magic not found");
    } else if length > 0x220000 {
        return Err("Slave FPGA gateware too large (corrupted?)");
    }
    info!("Slave FPGA gateware length: 0x{:06x}", length);

    unsafe {
        csr::slave_fpga_cfg::oe_write(CCLK_BIT | DIN_BIT | PROGRAM_B_BIT);

        csr::slave_fpga_cfg::out_write(0);
        clock::spin_us(1);  // TPROGRAM=250ns min
        csr::slave_fpga_cfg::out_write(PROGRAM_B_BIT);
        clock::spin_us(5_000);  // TPL=5ms max
        if csr::slave_fpga_cfg::in_read() & INIT_B_BIT == 0 {
            return Err("Slave FPGA did not initialize.");
        }

        for i in slice::from_raw_parts(GATEWARE.offset(8), length) {
            shift_u8(*i);
            if csr::slave_fpga_cfg::in_read() & INIT_B_BIT == 0 {
                return Err("Slave FPGA error: INIT_B went low.");
            }
        }

        let t = clock::get_ms();
        while csr::slave_fpga_cfg::in_read() & DONE_BIT == 0 {
            if clock::get_ms() > t + 100 {
                error!("Slave FPGA not DONE after loading");
                error!("Corrupt gateware? Slave FPGA in slave serial mode?");
                return Err("Slave FPGA not DONE");
            }
            shift_u8(0xff);
        }
        shift_u8(0xff);  // "Compensate for Special Startup Conditions"
        csr::slave_fpga_cfg::out_write(PROGRAM_B_BIT);
    }

    Ok(())
}
