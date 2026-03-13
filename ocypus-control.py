#!/usr/bin/env python3
"""
ocypus-control.py
---------------------------------
Ocypus Iota A40 / Gamma A40 Digital LCD driver (Linux / Proxmox)

FEATURES
  • Auto-detects the working HID interface.
  • Auto-detects CPU vendor (Intel/AMD) for sensor selection.
  • Supports both Iota and Gamma cooler protocols via --model argument.
  • Supports temperature display in Celsius (°C) and Fahrenheit (°F).
  • Works with any psutil sensor.
  • Keeps the panel alive with periodic updates.
  • Includes a command to generate and install a systemd service.
"""

import argparse
import hid
import os
import signal
import sys
import textwrap
import time
from types import FrameType
from typing import List, Dict, Any, Optional, Tuple

import psutil

# --- Protocol Constants ---
PROTOCOLS = {
    'iota': {
        'REPORT_ID': 0x07,
        'REPORT_LENGTH': 65,
        'USE_WRITE': False,  # Use send_feature_report()
        'MAGIC_BYTES': None,  # No magic bytes
        'TEMP_SLOTS': (5, 6),  # (tens, ones)
        'HAS_HUNDREDS': False,
        'UNIT_FLAG_SLOT': 7,
    },
    'gamma': {
        'REPORT_ID': 0x07,
        'REPORT_LENGTH': 64,
        'USE_WRITE': True,  # Use write()
        'MAGIC_BYTES': (1, 2),  # Positions for 0xff
        'TEMP_SLOTS': (3, 4, 5),  # (hundreds, tens, ones)
        'HAS_HUNDREDS': True,
        'UNIT_FLAG_SLOT': None,  # No unit flag
    }
}

VID, PID = 0x1a2c, 0x434d
DEFAULT_SENSOR_SUBSTR = None  # None triggers auto-detection
DEFAULT_REFRESH_RATE = 1.0
KEEPALIVE_INTERVAL = 2.0


def detect_cpu_vendor() -> Optional[str]:
    """
    Detects CPU vendor by reading /proc/cpuinfo.
    Returns: 'intel', 'amd', or None if unknown.
    """
    try:
        with open('/proc/cpuinfo', 'r') as f:
            content = f.read().lower()
            if 'genuineintel' in content:
                return 'intel'
            elif 'authenticamd' in content:
                return 'amd'
    except Exception as e:
        print(f"Warning: Could not detect CPU vendor: {e}", file=sys.stderr)
    return None


def get_default_sensor() -> str:
    """
    Returns the default sensor substring based on detected CPU vendor.
    Falls back to 'k10temp' (AMD) if detection fails.
    """
    vendor = detect_cpu_vendor()
    if vendor == 'intel':
        return 'coretemp'
    elif vendor == 'amd':
        return 'k10temp'
    else:
        print("Warning: CPU vendor unknown. Defaulting to 'k10temp'. Use -s to override.", file=sys.stderr)
        return 'k10temp'


class OcypusController:
    """Manages the Ocypus LCD device with protocol selection."""

    def __init__(self, model: str = 'iota'):
        self.device: Optional[hid.device] = None
        self.interface_number: Optional[int] = None
        self.model = model.lower()
        
        if self.model not in PROTOCOLS:
            raise ValueError(f"Unknown model: {self.model}. Supported: {', '.join(PROTOCOLS.keys())}")
        
        self.protocol = PROTOCOLS[self.model]

    def __enter__(self):
        """Context manager entry: opens the device."""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: closes the device."""
        self.close()

    def open(self) -> bool:
        """Opens the first working Ocypus device interface."""
        devices = hid.enumerate(VID, PID)
        if not devices:
            print("No Ocypus cooler found.")
            return False

        for device_info in devices:
            interface_number = device_info.get('interface_number')
            if interface_number is None:
                continue

            try:
                device = hid.device()
                device.open_path(device_info['path'])
                
                self.device = device
                self.interface_number = interface_number
                print(f"Connected to Ocypus cooler on interface {interface_number}")
                print(f"Using protocol: {self.model.upper()}")
                return True
            except Exception as e:
                print(f"Failed to open interface {interface_number}: {e}")
                try:
                    device.close()
                except:
                    pass
                continue

        print("Error: No working Ocypus interface found.")
        return False

    def close(self):
        """Closes the device connection."""
        if self.device:
            try:
                self.device.close()
            except Exception as e:
                print(f"Error closing device: {e}")
            finally:
                self.device = None
                self.interface_number = None

    def send_temperature(self, temp_celsius: float, unit: str = 'c') -> bool:
        """Sends temperature data to the LCD display using the selected protocol."""
        if not self.device:
            print("Device not connected.")
            return False

        # Convert temperature based on unit
        if unit.lower() == 'f':
            display_temp = temp_celsius * 9/5 + 32
        else:
            display_temp = temp_celsius

        # Clamp to displayable range
        display_temp = max(0, min(212, int(round(display_temp))))
        
        # Build report based on protocol
        report = [self.protocol['REPORT_ID']] + [0] * (self.protocol['REPORT_LENGTH'] - 1)
        
        # Add magic bytes if required (Gamma protocol)
        if self.protocol['MAGIC_BYTES']:
            for pos in self.protocol['MAGIC_BYTES']:
                report[pos] = 0xff
        
        # Add temperature values
        if self.protocol['HAS_HUNDREDS']:
            # Gamma: 3 bytes for hundreds, tens, ones
            hundreds = display_temp // 100
            tens = (display_temp % 100) // 10
            ones = display_temp % 10
            temp_slots = self.protocol['TEMP_SLOTS']
            report[temp_slots[0]] = hundreds
            report[temp_slots[1]] = tens
            report[temp_slots[2]] = ones
        else:
            # Iota: 2 bytes for tens, ones
            tens = display_temp // 10
            ones = display_temp % 10
            temp_slots = self.protocol['TEMP_SLOTS']
            report[temp_slots[0]] = tens
            report[temp_slots[1]] = ones
        
        # Add unit flag if required (Iota protocol)
        if self.protocol['UNIT_FLAG_SLOT'] is not None:
            unit_flag = 0x00 if unit.lower() == 'c' else 0x01
            report[self.protocol['UNIT_FLAG_SLOT']] = unit_flag
        
        try:
            # Send using the correct method for the protocol
            if self.protocol['USE_WRITE']:
                self.device.write(bytes(report))
            else:
                self.device.send_feature_report(report)
            return True
        except Exception as e:
            print(f"Error sending temperature: {e}")
            return False

    def blank_display(self) -> bool:
        """Blanks the LCD display."""
        if not self.device:
            print("Device not connected.")
            return False

        try:
            report = [self.protocol['REPORT_ID']] + [0] * (self.protocol['REPORT_LENGTH'] - 1)
            
            if self.protocol['USE_WRITE']:
                self.device.write(bytes(report))
            else:
                self.device.send_feature_report(report)
            return True
        except Exception as e:
            print(f"Error blanking display: {e}")
            return False

    def list_devices(self) -> List[Dict[str, Any]]:
        """Lists all Ocypus devices found."""
        devices = hid.enumerate(VID, PID)
        return devices


def get_temperature_sensors() -> Dict[str, List[Tuple[str, float]]]:
    """Gets all available temperature sensors."""
    try:
        return psutil.sensors_temperatures()
    except Exception as e:
        print(f"Error reading temperature sensors: {e}")
        return {}


def find_sensor_by_substring(sensors: Dict[str, List[Tuple[str, float]]], 
                           substring: str) -> Optional[Tuple[str, float]]:
    """Finds the first sensor containing the given substring."""
    for sensor_name, sensor_list in sensors.items():
        if substring.lower() in sensor_name.lower() and sensor_list:
            return sensor_name, sensor_list[0].current
    return None


def build_temperature_report(sensor_substring: str) -> str:
    """Builds a temperature report for debugging."""
    sensors = get_temperature_sensors()
    if not sensors:
        return "No temperature sensors found."
    
    report_lines = ["Available temperature sensors:"]
    for sensor_name, sensor_list in sensors.items():
        for sensor in sensor_list:
            temp_str = f"{sensor.current:.1f}°C"
            highlight = " ← SELECTED" if sensor_substring.lower() in sensor_name.lower() else ""
            report_lines.append(f"  {sensor_name}: {temp_str}{highlight}")
    
    return "\n".join(report_lines)


def run_display_loop(controller: OcypusController, 
                    sensor_substring: str, 
                    unit: str, 
                    refresh_rate: float):
    """Runs the main temperature display loop."""
    print(f"Starting temperature display (unit: {unit.upper()}, refresh: {refresh_rate}s)")
    print(f"Using sensor pattern: '{sensor_substring}'")
    print("Press Ctrl+C to stop.")
    
    last_keepalive = time.time()
    
    while True:
        try:
            # Get temperature
            sensors = get_temperature_sensors()
            sensor_data = find_sensor_by_substring(sensors, sensor_substring)
            
            if sensor_data:
                sensor_name, temp_celsius = sensor_data
                success = controller.send_temperature(temp_celsius, unit)
                if success:
                    display_temp = temp_celsius if unit.lower() == 'c' else temp_celsius * 9/5 + 32
                    unit_symbol = '°C' if unit.lower() == 'c' else '°F'
                    print(f"\rSensor: {sensor_name} | Temp: {display_temp:.1f}{unit_symbol}", end="", flush=True)
                else:
                    print("\rFailed to send temperature", end="", flush=True)
            else:
                print(f"\rSensor containing '{sensor_substring}' not found", end="", flush=True)
                # Send a keepalive to prevent display timeout
                current_time = time.time()
                if current_time - last_keepalive >= KEEPALIVE_INTERVAL:
                    controller.send_temperature(0, unit)
                    last_keepalive = current_time
            
            time.sleep(refresh_rate)
            
        except KeyboardInterrupt:
            print("\nStopping temperature display.")
            break
        except Exception as e:
            print(f"\nError in display loop: {e}")
            time.sleep(refresh_rate)


def install_systemd_service(unit: str = 'c', 
                          sensor: Optional[str] = None, 
                          rate: float = DEFAULT_REFRESH_RATE,
                          model: str = 'iota',
                          service_name: str = "ocypus-lcd"):
    """Creates and installs a systemd service unit."""
    script_path = os.path.abspath(__file__)
    
    # Resolve sensor for the service file
    effective_sensor = sensor if sensor else get_default_sensor()
    
    service_content = f"""[Unit]
Description=Ocypus LCD Temperature Display ({model.upper()} protocol)
After=multi-user.target

[Service]
Type=simple
User=root
ExecStart={sys.executable} {script_path} on -u {unit} -s "{effective_sensor}" -r {rate} --model {model}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    
    service_file_path = f"/etc/systemd/system/{service_name}.service"
    
    try:
        with open(service_file_path, 'w') as f:
            f.write(service_content)
        
        print(f"Systemd service created: {service_file_path}")
        print(f"Configured sensor: {effective_sensor} (auto-detected)")
        print(f"Configured model: {model.upper()}")
        print("\nTo enable and start the service:")
        print(f"  sudo systemctl daemon-reload")
        print(f"  sudo systemctl enable --now {service_name}.service")
        print("\nTo check service status:")
        print(f"  systemctl status {service_name}.service")
        
    except PermissionError:
        print(f"Error: Permission denied. Run with sudo to install the service.")
    except Exception as e:
        print(f"Error creating service file: {e}")


def main():
    """Main function with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Ocypus LCD driver for Linux/Proxmox with auto sensor detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          %(prog)s list                    # List all Ocypus devices
          %(prog)s on                      # Start display (auto-detect sensor, Iota protocol)
          %(prog)s on --model gamma        # Start display with Gamma protocol
          %(prog)s on -u f                 # Start display in Fahrenheit
          %(prog)s on -s "coretemp" -u c   # Force specific sensor
          %(prog)s off                     # Turn off display
          %(prog)s install-service         # Install systemd service
        """)
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # List command
    subparsers.add_parser('list', help='List all found Ocypus cooler devices')
    
    # On command
    on_parser = subparsers.add_parser('on', help='Turn on display and stream temperature')
    on_parser.add_argument('-u', '--unit', choices=['c', 'f'], default='c',
                          help='Temperature unit: c=Celsius, f=Fahrenheit (default: c)')
    on_parser.add_argument('-s', '--sensor', default=None,
                          help='Force specific sensor substring (default: auto-detect based on CPU)')
    on_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_REFRESH_RATE,
                          help=f'Update interval in seconds (default: {DEFAULT_REFRESH_RATE})')
    on_parser.add_argument('-m', '--model', choices=['iota', 'gamma'], default='iota',
                          help='Cooler model protocol: iota (default) or gamma')
    
    # Off command
    off_parser = subparsers.add_parser('off', help='Turn off (blank) the display')
    off_parser.add_argument('-m', '--model', choices=['iota', 'gamma'], default='iota',
                           help='Cooler model protocol: iota (default) or gamma')
    
    # Install service command
    service_parser = subparsers.add_parser('install-service', 
                                          help='Install systemd unit for background operation')
    service_parser.add_argument('-u', '--unit', choices=['c', 'f'], default='c',
                               help='Temperature unit for the service (default: c)')
    service_parser.add_argument('-s', '--sensor', default=None,
                               help='Force specific sensor for the service (default: auto-detect)')
    service_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_REFRESH_RATE,
                               help=f'Update interval for the service (default: {DEFAULT_REFRESH_RATE})')
    service_parser.add_argument('-m', '--model', choices=['iota', 'gamma'], default='iota',
                               help='Cooler model protocol for the service (default: iota)')
    service_parser.add_argument('--name', default='ocypus-lcd',
                               help='Name for the systemd unit file (default: ocypus-lcd)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Set up signal handling for graceful shutdown
    def signal_handler(signum: int, frame: Optional[FrameType]):
        print("\nReceived interrupt signal. Exiting gracefully...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if args.command == 'list':
        controller = OcypusController(model=args.model if hasattr(args, 'model') else 'iota')
        devices = controller.list_devices()
        if devices:
            print(f"Found {len(devices)} Ocypus cooler device(s):")
            for i, device in enumerate(devices, 1):
                interface = device.get('interface_number', 'Unknown')
                path = device.get('path', 'Unknown')
                print(f"  {i}. Interface {interface} (Path: {path.decode() if isinstance(path, bytes) else path})")
        else:
            print("No Ocypus cooler devices found.")
    
    elif args.command == 'on':
        # Resolve sensor: use provided argument or auto-detect
        sensor_substring = args.sensor if args.sensor else get_default_sensor()
        
        with OcypusController(model=args.model) as controller:
            if controller.device:
                run_display_loop(controller, sensor_substring, args.unit, args.rate)
    
    elif args.command == 'off':
        with OcypusController(model=args.model) as controller:
            if controller.device:
                success = controller.blank_display()
                if success:
                    print("Display turned off.")
                else:
                    print("Failed to turn off display.")
    
    elif args.command == 'install-service':
        install_systemd_service(args.unit, args.sensor, args.rate, args.model, args.name)


if __name__ == "__main__":
    main()