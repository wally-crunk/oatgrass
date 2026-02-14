"""
Minimal logging context for Oatgrass.
Single place to control all output: screen + file, with flush.
"""
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

class OatgrassLogger:
    """Minimal logger: print to screen + file, always flush"""
    
    def __init__(self, log_file: Optional[Path] = None, debug: bool = False):
        self.log_file = log_file
        self._file_handle = None
        self._start_time = datetime.now()
        self.debug_mode = debug
        self._rate_limit_note_trackers: set[str] = set()
        
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(log_file, 'w', buffering=1, encoding='utf-8')  # Line buffered, UTF-8
        
        # Welcome message
        try:
            import oatgrass
            version = getattr(oatgrass, '__version__', '0.0.0')
        except:
            version = '0.0.0'
        
        welcome = f"({self._start_time.strftime('%H:%M:%S')}  Started Oatgrass {version})"
        self.log(welcome)
    
    def log(self, msg: str, prefix: str = ""):
        """Log to screen and file"""
        output = f"{prefix}{msg}" if prefix else msg
        
        # Screen (unbuffered)
        print(output, flush=True)
        sys.stdout.flush()  # Force flush
        
        # File
        if self._file_handle:
            self._file_handle.write(output + "\n")
            self._file_handle.flush()  # Force flush
            import os
            os.fsync(self._file_handle.fileno())  # Force OS write
    
    def info(self, msg: str):
        """Info message"""
        self.log(msg)
    
    def warning(self, msg: str):
        """Warning message"""
        self.log(msg, "[WARNING] ")
    
    def error(self, msg: str):
        """Error message"""
        self.log(msg, "[ERROR] ")
    
    def api_wait(self, tracker: str, seconds: float):
        """Log API rate limiting wait"""
        _ = seconds
        tracker_key = tracker.upper()
        if tracker_key in self._rate_limit_note_trackers:
            return
        self._rate_limit_note_trackers.add(tracker_key)
        self.log(
            f"API rate limiting active for {tracker_key}; request pacing is enabled.",
            "[INFO] ",
        )

    def api_wait_debug(self, tracker: str, seconds: float):
        """Log API wait details (debug mode only)."""
        self.debug(f"Rate limiting detail: waiting {seconds:.3f}s before next {tracker} API call")
    
    def api_retry(self, tracker: str, attempt: int, max_attempts: int, delay: int):
        """Log API retry"""
        self.log(f"{tracker} server timeout. Retrying in {delay}s... (attempt {attempt}/{max_attempts})", "[WARNING] ")
    
    def api_failed(self, tracker: str, max_attempts: int):
        """Log API failure"""
        self.log(f"{tracker} server not responding after {max_attempts} attempts. Aborting.", "[ERROR] ")
    
    def debug(self, msg: str):
        """Debug message (only shown in debug mode)"""
        if self.debug_mode:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.log(msg, f"[{timestamp}] [DEBUG] ")
    
    def api_request(self, method: str, url: str, params: dict):
        """Log API request (debug mode only)"""
        if self.debug_mode:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.log(f"API Request: {method} {url}", f"[{timestamp}] ")
            if params:
                import json
                self.log(f"  Params: {json.dumps(params, indent=2)}", f"[{timestamp}] ")
    
    def api_response(self, status: int, data: dict, elapsed_ms: float):
        """Log API response (debug mode only)"""
        if self.debug_mode:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.log(f"API Response ({elapsed_ms:.0f}ms): Status {status}", f"[{timestamp}] ")
            if data:
                import json
                # Truncate large responses
                data_str = json.dumps(data, indent=2)
                if len(data_str) > 5000:
                    data_str = data_str[:5000] + "\n  ... (truncated)"
                self.log(f"  Data: {data_str}", f"[{timestamp}] ")
    
    def close(self):
        """Close file handle with goodbye message"""
        if self._file_handle:
            end_time = datetime.now()
            elapsed = end_time - self._start_time
            goodbye = f"({end_time.strftime('%H:%M:%S')}  Ended session, elapsed {elapsed.total_seconds():.1f}s)"
            self.log(goodbye)
            self._file_handle.close()
            self._file_handle = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


# Global instance (set by search mode)
_logger: Optional[OatgrassLogger] = None

def set_logger(logger: OatgrassLogger):
    """Set global logger instance"""
    global _logger
    _logger = logger

def get_logger() -> OatgrassLogger:
    """Get global logger instance"""
    global _logger
    if _logger is None:
        # Fallback: create stdout-only logger
        _logger = OatgrassLogger()
    return _logger

# Convenience functions
def log(msg: str):
    get_logger().log(msg)

def info(msg: str):
    get_logger().info(msg)

def warning(msg: str):
    get_logger().warning(msg)

def error(msg: str):
    get_logger().error(msg)
