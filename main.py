import webview
import threading
from flask import Flask, render_template, jsonify, request
import serial
import serial.tools.list_ports
import time
import logging
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import atexit
import signal
from collections import deque

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Silence Flask logs
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Create Flask app
app = Flask(__name__, static_folder='static', template_folder='templates')

# Global variables
serial_conn = None
serial_thread = None
is_reading = False
flask_thread = None
flask_server = None

# Temperature averaging buffers
temp_motor_buffer = deque(maxlen=25)
temp_esc_buffer = deque(maxlen=25)
torque_buffer = deque(maxlen=10)

current_data = {
    'temp_motor': 0.0,
    'temp_esc': 0.0,
    'torque': 0.0,
    'rpm': 0.0,
    'timestamp': None
}
data_history = []
MAX_HISTORY = 1000000

# Load cell dimensions in meters
LOAD_CELL_WIDTH = 12.65 / 1000  # 12.65 mm to meters
DISTANCE_BETWEEN_EXTREMES = 7.5 / 100  # 7.5 cm to meters

# Calculate distance from center to each load cell
DISTANCE_FROM_CENTER = (DISTANCE_BETWEEN_EXTREMES - LOAD_CELL_WIDTH) / 2

# RPM filtering threshold
RPM_THRESHOLD = 300  # RPM values below this will be shown as 0

def filter_rpm_value(rpm_raw):
    """Filter RPM values below threshold"""
    if rpm_raw < RPM_THRESHOLD:
        return 0.0
    return rpm_raw

def calculate_average(buffer, new_value):
    """Calculate running average of values in buffer"""
    buffer.append(new_value)
    if len(buffer) == 0:
        return new_value
    return sum(buffer) / len(buffer)

def parse_serial_data(line):
    """Parse serial data from ESP32 with flexible format handling"""
    try:
        # Remove any whitespace
        line = line.strip()
        if not line:
            return None
            
        # Check for special commands
        if line == "TARE_OK":
            logger.info("Tare complete received")
            return None
        
        # Parse comma-separated values
        parts = line.split(',')
        parts = [p.strip() for p in parts if p.strip()]  # Clean up parts
        
        # We need at least 2 values (temp_motor and temp_esc)
        if len(parts) < 2:
            logger.warning(f"Too few values: {len(parts)} in line: {line}")
            return None
        
        # Parse temperature values (always first 2)
        try:
            temp_motor_raw = float(parts[0])
            temp_esc_raw = float(parts[1])
        except ValueError:
            logger.warning(f"Cannot parse temperatures from: {parts[0]}, {parts[1]}")
            return None
        
        # Apply averaging to temperature readings
        temp_motor = (calculate_average(temp_motor_buffer, temp_motor_raw))*2
        temp_esc = (calculate_average(temp_esc_buffer, temp_esc_raw))*2
        
        # Initialize default values
        left_load = 0.0
        right_load = 0.0
        rpm = 0.0
        
        # Parse additional values if available
        if len(parts) >= 3:
            try:
                # Try to parse as load cell value
                left_load = float(parts[2])
            except ValueError:
                logger.debug(f"Cannot parse left load value: {parts[2]}")
                
        if len(parts) >= 4:
            try:
                # Try to parse as load cell value
                right_load = float(parts[3])
            except ValueError:
                logger.debug(f"Cannot parse right load value: {parts[3]}")
                
        if len(parts) >= 5:
            try:
                # Try to parse as RPM
                rpm_raw = float(parts[4])
                # Filter out RPM below threshold
                rpm = filter_rpm_value(rpm_raw)
            except ValueError:
                logger.debug(f"Cannot parse RPM value: {parts[4]}")
        
        # Calculate torque from load cell values (absolute value only)
        torque_raw = abs((left_load * DISTANCE_FROM_CENTER) - (right_load * DISTANCE_FROM_CENTER))

        # 5 point moving average for torque
        torque = calculate_average(torque_buffer, torque_raw)
        
        return {
            'temp_motor': temp_motor,
            'temp_esc': temp_esc,
            'torque': torque,
            'rpm': rpm
        }
        
    except ValueError as e:
        logger.error(f"ValueError parsing data '{line}': {e}")
    except Exception as e:
        logger.error(f"Error parsing data '{line}': {e}")
    return None

def send_tare():
    """Send tare command"""
    if serial_conn and serial_conn.is_open:
        try:
            serial_conn.write(b"T\n")
            logger.info("Sent tare command")
            return True
        except Exception as e:
            logger.error(f"Tare error: {e}")
            disconnect_serial()
            return False
    return False

@app.route('/api/tare', methods=['POST'])
def api_tare():
    success = send_tare()
    return jsonify({'success': success, 'message': 'Tare command sent successfully'})

def serial_reader():
    """Background thread to read serial data"""
    global serial_conn, is_reading, current_data, data_history
    
    while is_reading and serial_conn and serial_conn.is_open:
        try:
            if serial_conn.in_waiting > 0:
                line = serial_conn.readline().decode('utf-8', errors='ignore').strip()
                if line:
                    logger.debug(f"Received: {line}")
                
                parsed_data = parse_serial_data(line)
                if parsed_data:
                    current_data.update(parsed_data)
                    current_data['timestamp'] = datetime.now().isoformat()
                    
                    data_history.append(current_data.copy())
                    if len(data_history) > MAX_HISTORY:
                        data_history.pop(0)
                    
        except serial.SerialException as e:
            logger.error(f"Serial port error: {e}")
            is_reading = False
            break
        except Exception as e:
            logger.error(f"Serial read error: {e}")
            time.sleep(0.1)
    
    logger.info("Serial reader stopped")

def connect_serial(port, baudrate=115200):
    """Connect to serial port"""
    global serial_conn, serial_thread, is_reading, temp_motor_buffer, temp_esc_buffer, torque_buffer
    
    if not port:
        return False, "No port specified"
    
    try:
        # Close existing connection
        if serial_conn and serial_conn.is_open:
            disconnect_serial()
            time.sleep(0.5)
        
        # Clear temperature buffers for new connection
        temp_motor_buffer.clear()
        temp_esc_buffer.clear()
        torque_buffer.clear()

        # Connect to new port
        serial_conn = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=1,
            write_timeout=1
        )
        
        # Clear buffers
        serial_conn.reset_input_buffer()
        serial_conn.reset_output_buffer()
        
        time.sleep(2)  # Wait for connection stabilization
        
        # Start reading thread
        is_reading = True
        serial_thread = threading.Thread(target=serial_reader, daemon=True)
        serial_thread.start()
        
        logger.info(f"Connected to {port} at {baudrate} baud")
        return True, f"Connected to {port} at {baudrate} baud"
        
    except serial.SerialException as e:
        logger.error(f"Serial connection error: {e}")
        return False, f"Failed to connect: {str(e)}"
    except Exception as e:
        logger.error(f"Connection error: {e}")
        return False, f"Unexpected error: {str(e)}"

def disconnect_serial():
    """Disconnect from serial port"""
    global serial_conn, is_reading, temp_motor_buffer, temp_esc_buffer, torque_buffer
    
    is_reading = False
    if serial_thread:
        serial_thread.join(timeout=1)
    
    if serial_conn and serial_conn.is_open:
        try:
            serial_conn.close()
        except:
            pass
        serial_conn = None
    
    # Clear temperature buffers on disconnect
    temp_motor_buffer.clear()
    temp_esc_buffer.clear()
    torque_buffer.clear()
    
    logger.info("Disconnected")
    return True

def test_serial_connection(port, baudrate):
    """Test if serial port is available"""
    try:
        test_conn = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=1
        )
        test_conn.close()
        return True
    except:
        return False

# Flask Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/connect', methods=['POST'])
def api_connect():
    data = request.json
    port = data.get('port')
    baudrate = data.get('baudrate', 115200)
    
    if not port:
        return jsonify({'success': False, 'message': 'Please select a COM port'})
    
    success, message = connect_serial(port, baudrate)
    return jsonify({'success': success, 'message': message})

@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    success = disconnect_serial()
    return jsonify({'success': success, 'message': 'Disconnected'})

@app.route('/api/ports', methods=['GET'])
def api_ports():
    ports = []
    try:
        available_ports = serial.tools.list_ports.comports()
        for port in available_ports:
            ports.append({
                'device': port.device,
                'name': port.name,
                'description': port.description or 'Unknown',
                'manufacturer': port.manufacturer or 'Unknown'
            })
    except Exception as e:
        logger.error(f"Error getting ports: {e}")
    
    return jsonify({'ports': ports})

@app.route('/api/connection_status', methods=['GET'])
def api_connection_status():
    connected = serial_conn is not None and serial_conn.is_open
    status = {
        'connected': connected,
        'port': serial_conn.port if connected else None,
        'baudrate': serial_conn.baudrate if connected else None
    }
    return jsonify(status)

@app.route('/api/test_connection', methods=['POST'])
def api_test_connection():
    data = request.json
    port = data.get('port')
    baudrate = data.get('baudrate', 115200)
    
    if not port:
        return jsonify({'success': False, 'message': 'No port specified'})
    
    success = test_serial_connection(port, baudrate)
    return jsonify({'success': success, 'message': f'Port {port} is available' if success else f'Cannot access port {port}'})

@app.route('/api/data', methods=['GET'])
def api_data():
    return jsonify(current_data)

@app.route('/api/history', methods=['GET'])
def api_history():
    return jsonify(data_history[-100:])  # Return last 100 points

@app.route('/api/save_csv', methods=['POST'])
def save_csv():
    """Save data as CSV directly to Downloads folder"""
    import csv

    if not data_history:
        return jsonify({'success': False, 'message': 'No data to export'})

    try:
        # Get the user's Downloads folder cross-platform
        downloads_path = str(Path.home() / "Downloads")
        filename = f"BLDC_Test_Data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        full_path = os.path.join(downloads_path, filename)

        with open(full_path, 'w', newline='') as f:
            writer = csv.writer(f)
            # Write header
            writer.writerow(['Timestamp', 'Motor Temp (C)', 'ESC Temp (C)', 'Torque (Nm)', 'RPM'])

            # Write data
            for entry in data_history:
                writer.writerow([
                    entry.get('timestamp', ''),
                    entry.get('temp_motor', 0),
                    entry.get('temp_esc', 0),
                    entry.get('torque', 0),
                    entry.get('rpm', 0)
                ])

        return jsonify({'success': True, 'message': f'File saved to: {filename}'})

    except Exception as e:
        logger.error(f"Export error: {e}")
        return jsonify({'success': False, 'message': f'Export failed: {str(e)}'})

@app.route('/api/clear', methods=['POST'])
def clear_data():
    global data_history, temp_motor_buffer, temp_esc_buffer, torque_buffer
    data_history = []
    # Clear temperature buffers when data is cleared
    temp_motor_buffer.clear()
    temp_esc_buffer.clear()
    torque_buffer.clear()
    return jsonify({'success': True, 'message': 'Data cleared'})

class FlaskServer:
    """Custom Flask server with proper shutdown capability"""
    def __init__(self, app, host='127.0.0.1', port=5001):
        self.app = app
        self.host = host
        self.port = port
        self.server = None
        self._stop_event = threading.Event()
        
    def run(self):
        """Run Flask server with Werkzeug"""
        from werkzeug.serving import make_server
        
        self.server = make_server(self.host, self.port, self.app, threaded=True)
        logger.info(f"Flask server started on http://{self.host}:{self.port}")
        
        # Start the server
        self.server.serve_forever()
        
    def shutdown(self):
        """Shutdown the Flask server"""
        if self.server:
            logger.info("Shutting down Flask server...")
            self.server.shutdown()
            logger.info("Flask server stopped")

def run_flask():
    """Run Flask server in background"""
    global flask_server
    flask_server = FlaskServer(app)
    flask_server.run()

def cleanup():
    """Cleanup function to be called on exit"""
    logger.info("Cleaning up resources...")
    
    # Disconnect serial
    if serial_conn and serial_conn.is_open:
        disconnect_serial()
    
    # Stop Flask server
    if flask_server:
        flask_server.shutdown()
    
    logger.info("Cleanup complete")

def signal_handler(signum, frame):
    """Handle termination signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    cleanup()
    sys.exit(0)

def main():
    """Main function to start PyWebView"""
    logger.info("Starting BLDC Motor Testing System...")
    
    # Register cleanup function
    atexit.register(cleanup)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start Flask server in background thread
    global flask_thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Wait for Flask to start
    time.sleep(1)
    
    # Create PyWebView window
    window = webview.create_window(
        'BLDC Motor Testing System',
        'http://127.0.0.1:5001',
        width=1200,
        height=800,
        resizable=True,
        fullscreen=False,
        min_size=(800, 600)
    )
    
    try:
        # Start webview
        webview.start(debug=False)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Error in webview: {e}")
    finally:
        # Cleanup when webview closes
        cleanup()
    
    logger.info("Application closed")

if __name__ == '__main__':
    main()