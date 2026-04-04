#!/usr/bin/env python3
"""
ocypus-control.py
---------------------------------
Ocypus Iota A40 / Gamma A40 Digital LCD driver (Linux / Proxmox)

FEATURES
  • Auto-detects the working HID interface.
  • Hardware-independent sensor reading via sysfs hwmon interface.
  • Defaults to Tccd1 (actual CCD temperature) when available.
  • Supports both Iota and Gamma cooler protocols via --model argument.
  • Supports temperature display in Celsius (°C) and Fahrenheit (°F).
  • Flexible sensor selection by label substring.
  • Keeps the panel alive with periodic updates.
  • Includes a command to generate and install a systemd service.
"""

import argparse
import glob
import hid
import os
import signal
import sys
import textwrap
import time
from types import FrameType
from typing import List, Dict, Any, Optional, Tuple

# --- Protocol Constants ---
PROTOCOLS = {
    'iota': {
        'REPORT_ID': 0x07,
        'REPORT_LENGTH': 65,
        'USE_WRITE': False,
        'MAGIC_BYTES': None,  # No magic bytes
        'TEMP_SLOTS': (5, 6),  # (tens, ones)
        'HAS_HUNDREDS': False,
        'UNIT_FLAG_SLOT': 7,
    },
    'gamma': {
        'REPORT_ID': 0x07,
        'REPORT_LENGTH': 64,
        'USE_WRITE': True,
        'MAGIC_BYTES': (1, 2),  # Positions for 0xff
        'TEMP_SLOTS': (3, 4, 5),  # (hundreds, tens, ones)
        'HAS_HUNDREDS': True,
        'UNIT_FLAG_SLOT': None,
    }
}

VID, PID = 0x1a2c, 0x434d
DEFAULT_REFRESH_RATE = 1.0
KEEPALIVE_INTERVAL = 2.0

# Sensor selection priority (first match wins)
SENSOR_PRIORITY = [
    'Tccd1',      # AMD CCD1 temperature (most accurate for AMD Ryzen)
    'Tccd2',      # AMD CCD2 temperature
    'Tdie',       # AMD die temperature (some older Ryzen)
    'Package',    # Intel package temperature
    'Tctl',       # AMD control temperature (may be offset on certain CPU models)
    'Core 0',     # Intel core temperature
]


class HwmonSensor:
    """Represents a single hwmon temperature sensor."""
    
    def __init__(self, hwmon_path: str, chip_name: str, base: str, 
                 label: Optional[str], value: float):
        self.hwmon_path = hwmon_path
        self.chip_name = chip_name
        self.base = base
        self.label = label
        self.value = value
    
    @property
    def identifier(self) -> str:
        """Returns a human-readable identifier for this sensor."""
        if self.label:
            return f"{self.chip_name}/{self.label}"
        return f"{self.chip_name}/{self.base}"


def read_hwmon_sensors() -> List[HwmonSensor]:
    """
    Reads all temperature sensors from sysfs hwmon interface.
    This is hardware-independent - works with any hwmon driver.
    """
    sensors = []
    
    for hwmon_path in sorted(glob.glob('/sys/class/hwmon/hwmon*')):
        try:
            with open(os.path.join(hwmon_path, 'name'), 'r') as f:
                chip_name = f.read().strip()
        except (IOError, OSError):
            continue
        
        for temp_input_path in glob.glob(os.path.join(hwmon_path, 'temp*_input')):
            base = os.path.basename(temp_input_path).replace('_input', '')
            
            try:
                with open(temp_input_path, 'r') as f:
                    temp_milli = int(f.read().strip())
                    temp_celsius = temp_milli / 1000.0
            except (IOError, OSError, ValueError):
                continue
            
            if temp_celsius < -55 or temp_celsius > 150:
                continue
            
            label = None
            label_path = os.path.join(hwmon_path, f'{base}_label')
            if os.path.exists(label_path):
                try:
                    with open(label_path, 'r') as f:
                        label = f.read().strip()
                except (IOError, OSError):
                    pass
            
            sensors.append(HwmonSensor(
                hwmon_path=hwmon_path,
                chip_name=chip_name,
                base=base,
                label=label,
                value=temp_celsius
            ))
    
    return sensors


def find_sensor_by_pattern(sensors: List[HwmonSensor], 
                           pattern: str) -> Optional[HwmonSensor]:
    """Finds the first sensor whose identifier matches the pattern (case-insensitive)."""
    pattern_lower = pattern.lower()
    
    for sensor in sensors:
        if sensor.label and pattern_lower in sensor.label.lower():
            return sensor
        if pattern_lower in sensor.chip_name.lower():
            return sensor
        if pattern_lower in sensor.base.lower():
            return sensor
    
    return None


def find_best_sensor(sensors: List[HwmonSensor]) -> Optional[HwmonSensor]:
    """
    Finds the best temperature sensor using priority list.
    Tccd1 (actual CCD temperature) is preferred over Tctl (control temp with offset).
    """
    if not sensors:
        return None
    
    for preferred in SENSOR_PRIORITY:
        for sensor in sensors:
            label = (sensor.label or '').lower()
            if preferred.lower() == label.lower():
                return sensor
    
    for sensor in sensors:
        if sensor.label:
            return sensor
    
    return sensors[0]


def build_sensor_report(sensors: List[HwmonSensor], 
                        selected_pattern: Optional[str] = None) -> str:
    """Builds a formatted report of all available sensors."""
    if not sensors:
        return "No temperature sensors found in /sys/class/hwmon/"
    
    selected = None
    if selected_pattern:
        selected = find_sensor_by_pattern(sensors, selected_pattern)
    else:
        selected = find_best_sensor(sensors)
    
    lines = ["Available temperature sensors (from /sys/class/hwmon/):", ""]
    
    current_chip = None
    for sensor in sensors:
        if sensor.chip_name != current_chip:
            current_chip = sensor.chip_name
            lines.append(f"  [{current_chip}]")
        
        temp_str = f"{sensor.value:.1f}°C"
        label_str = sensor.label if sensor.label else f"(no label: {sensor.base})"
        
        if selected and sensor is selected:
            highlight = " ← SELECTED (default)"
        else:
            highlight = ""
        
        lines.append(f"    {label_str}: {temp_str}{highlight}")
    
    lines.append("")
    lines.append("Selection priority: " + " > ".join(SENSOR_PRIORITY))
    
    return "\n".join(lines)


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
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def open(self) -> bool:
        devices = hid.enumerate(VID, PID)
        if not devices:
            print("No Ocypus cooler found.")
            return False

        permission_denied = False

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
                
            except OSError as e:
                err_str = str(e).lower()
                # Detect USB permission errors
                if 'permission' in err_str or 'open failed' in err_str or 'access' in err_str:
                    permission_denied = True
                print(f"Failed to open interface {interface_number}: {e}")
                try:
                    device.close()
                except:
                    pass
                continue
                
            except Exception as e:
                print(f"Failed to open interface {interface_number}: {e}")
                try:
                    device.close()
                except:
                    pass
                continue

        # If we failed purely due to permissions, print helpful instructions
        if permission_denied:
            print("\n" + "="*65)
            print(" PERMISSION DENIED: USB Device Access Error")
            print("="*65)
            print(" Linux blocks access to raw USB devices for non-root users.")
            print(" You can fix this permanently using one of these methods:\n")
            print(" Option 1: Install a udev rule (Recommended)")
            print(" --------------------------------------------------")
            print(f" echo 'KERNEL==\"hidraw*\", ATTRS{{idVendor}}==\"{VID:04x}\", ATTRS{{idProduct}}==\"{PID:04x}\", MODE=\"0666\"' | sudo tee /etc/udev/rules.d/99-ocypus.rules")
            print(" sudo udevadm control --reload-rules")
            print(" sudo udevadm trigger")
            print("\n Option 2: Run with sudo")
            print(" --------------------------------------------------")
            print(f" sudo $(which python3) {os.path.basename(__file__)} on -m {self.model}")
            print("="*65)
            return False

        print("Error: No working Ocypus interface found.")
        return False

    def close(self):
        if self.device:
            try:
                self.device.close()
            except Exception as e:
                print(f"Error closing device: {e}")
            finally:
                self.device = None
                self.interface_number = None

    def send_temperature(self, temp_celsius: float, unit: str = 'c') -> bool:
        if not self.device:
            print("Device not connected.")
            return False

        if unit.lower() == 'f':
            display_temp = temp_celsius * 9/5 + 32
        else:
            display_temp = temp_celsius

        display_temp = max(0, min(212, int(round(display_temp))))
        
        report = [self.protocol['REPORT_ID']] + [0] * (self.protocol['REPORT_LENGTH'] - 1)
        
        if self.protocol['MAGIC_BYTES']:
            for pos in self.protocol['MAGIC_BYTES']:
                report[pos] = 0xff
        
        if self.protocol['HAS_HUNDREDS']:
            hundreds = display_temp // 100
            tens = (display_temp % 100) // 10
            ones = display_temp % 10
            temp_slots = self.protocol['TEMP_SLOTS']
            report[temp_slots[0]] = hundreds
            report[temp_slots[1]] = tens
            report[temp_slots[2]] = ones
        else:
            tens = display_temp // 10
            ones = display_temp % 10
            temp_slots = self.protocol['TEMP_SLOTS']
            report[temp_slots[0]] = tens
            report[temp_slots[1]] = ones
        
        if self.protocol['UNIT_FLAG_SLOT'] is not None:
            unit_flag = 0x00 if unit.lower() == 'c' else 0x01
            report[self.protocol['UNIT_FLAG_SLOT']] = unit_flag
        
        try:
            if self.protocol['USE_WRITE']:
                self.device.write(bytes(report))
            else:
                self.device.send_feature_report(report)
            return True
        except Exception as e:
            print(f"Error sending temperature: {e}")
            return False

    def blank_display(self) -> bool:
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
        return hid.enumerate(VID, PID)


def run_display_loop(controller: OcypusController, 
                    sensor_pattern: Optional[str], 
                    unit: str, 
                    refresh_rate: float):
    """Runs the main temperature display loop."""
    
    resolved_sensor: Optional[HwmonSensor] = None
    use_auto_select = sensor_pattern is None
    
    print(f"Starting temperature display (unit: {unit.upper()}, refresh: {refresh_rate}s)")
    if use_auto_select:
        print(f"Sensor: auto-select (priority: Tccd1 > Tccd2 > Tdie > Package > Tctl > Core 0)")
    else:
        print(f"Sensor pattern: '{sensor_pattern}'")
    print("Press Ctrl+C to stop.")
    
    last_keepalive = time.time()
    
    while True:
        try:
            sensors = read_hwmon_sensors()
            
            if not sensors:
                print("\rNo sensors found in /sys/class/hwmon/", end="", flush=True)
                time.sleep(refresh_rate)
                continue
            
            if resolved_sensor is None:
                if use_auto_select:
                    resolved_sensor = find_best_sensor(sensors)
                else:
                    resolved_sensor = find_sensor_by_pattern(sensors, sensor_pattern)
                
                if resolved_sensor:
                    print(f"\nUsing sensor: {resolved_sensor.identifier}")
                else:
                    if not use_auto_select:
                        print(f"\nSensor matching '{sensor_pattern}' not found!")
                    else:
                        print("\nNo suitable sensor found!")
            
            if resolved_sensor:
                current_sensor = find_sensor_by_pattern(sensors, resolved_sensor.label or resolved_sensor.chip_name)
                
                if current_sensor:
                    temp_celsius = current_sensor.value
                    success = controller.send_temperature(temp_celsius, unit)
                    
                    if success:
                        display_temp = temp_celsius if unit.lower() == 'c' else temp_celsius * 9/5 + 32
                        unit_symbol = '°C' if unit.lower() == 'c' else '°F'
                        print(f"\r{resolved_sensor.identifier}: {display_temp:.1f}{unit_symbol}  ", 
                              end="", flush=True)
                    else:
                        print("\rFailed to send temperature  ", end="", flush=True)
            
            current_time = time.time()
            if current_time - last_keepalive >= KEEPALIVE_INTERVAL:
                if resolved_sensor is None:
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
    sensor_arg = f'-s "{sensor}"' if sensor else ""
    
    service_content = f"""[Unit]
Description=Ocypus LCD Temperature Display ({model.upper()} protocol)
After=multi-user.target

[Service]
Type=simple
User=root
ExecStart={sys.executable} {script_path} on -u {unit} {sensor_arg} -r {rate} -m {model}
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
        print(f"Configured model: {model.upper()}")
        if sensor:
            print(f"Configured sensor: {sensor}")
        else:
            print("Sensor: auto-select (defaults to Tccd1 for AMD, Package for Intel)")
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
    parser = argparse.ArgumentParser(
        description="Ocypus LCD driver for Linux/Proxmox (hardware-independent sensor reading)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Sensor Selection:
          By default, the driver auto-selects the best sensor using this priority:
            Tccd1 (AMD) > Tccd2 (AMD) > Tdie (AMD) > Package (Intel) > Tctl (AMD) > Core 0 (Intel)
          
          Tccd1 is the actual CCD1 die temperature on AMD Ryzen processors.
          Tctl is the control temperature which may be offset on certain CPU models.
          
          Use -s to override with any sensor label substring (case-insensitive).
          Use --list-sensors to see all available sensors.

        Examples:
          %(prog)s list                        # List Ocypus devices
          %(prog)s --list-sensors              # List all temperature sensors
          %(prog)s on                          # Start display (auto-select sensor)
          %(prog)s on -m gamma                 # Start with Gamma protocol
          %(prog)s on -u f                     # Display in Fahrenheit
          %(prog)s on -s "Tccd1"               # Force Tccd1 sensor
          %(prog)s on -s "Package"             # Force Intel Package sensor
          %(prog)s off                         # Turn off display
          %(prog)s off -m gamma                # Turn off Gamma display
          %(prog)s install-service             # Install systemd service
          %(prog)s install-service -m gamma    # Install service for Gamma
        """)
    )
    
    parser.add_argument('--list-sensors', action='store_true',
                       help='List all available temperature sensors and exit')
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # List command
    subparsers.add_parser('list', help='List all found Ocypus cooler devices')
    
    # On command
    on_parser = subparsers.add_parser('on', help='Turn on display and stream temperature')
    on_parser.add_argument('-u', '--unit', choices=['c', 'f'], default='c',
                          help='Temperature unit: c=Celsius, f=Fahrenheit (default: c)')
    on_parser.add_argument('-s', '--sensor', default=None,
                          help='Sensor label substring to match (default: auto-select)')
    on_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_REFRESH_RATE,
                          help=f'Update interval in seconds (default: {DEFAULT_REFRESH_RATE})')
    on_parser.add_argument('-m', '--model', choices=['iota', 'gamma'], default='iota',
                          help='Cooler model protocol (default: iota)')
    
    # Off command
    off_parser = subparsers.add_parser('off', help='Turn off (blank) the display')
    off_parser.add_argument('-m', '--model', choices=['iota', 'gamma'], default='iota',
                           help='Cooler model protocol (default: iota)')
    
    # Install service command
    service_parser = subparsers.add_parser('install-service', 
                                          help='Install systemd unit for background operation')
    service_parser.add_argument('-u', '--unit', choices=['c', 'f'], default='c',
                               help='Temperature unit for the service (default: c)')
    service_parser.add_argument('-s', '--sensor', default=None,
                               help='Sensor label substring for the service (default: auto-select)')
    service_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_REFRESH_RATE,
                               help=f'Update interval for the service (default: {DEFAULT_REFRESH_RATE})')
    service_parser.add_argument('-m', '--model', choices=['iota', 'gamma'], default='iota',
                               help='Cooler model protocol for the service (default: iota)')
    service_parser.add_argument('--name', default='ocypus-lcd',
                               help='Name for the systemd unit file (default: ocypus-lcd)')
    
    args = parser.parse_args()
    
    # Handle --list-sensors
    if args.list_sensors:
        sensors = read_hwmon_sensors()
        print(build_sensor_report(sensors))
        return
    
    if not args.command:
        parser.print_help()
        return
    
    def signal_handler(signum: int, frame: Optional[FrameType]):
        print("\nReceived interrupt signal. Exiting gracefully...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if args.command == 'list':
        controller = OcypusController(model='iota')
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
        with OcypusController(model=args.model) as controller:
            if controller.device:
                run_display_loop(controller, args.sensor, args.unit, args.rate)
    
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