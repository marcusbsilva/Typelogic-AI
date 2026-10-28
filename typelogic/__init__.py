import os
# Native telemetry must be disabled before ONNX Runtime is imported.
os.environ['ORT_DISABLE_TELEMETRY'] = '1'
__version__ = '1.1.0'
