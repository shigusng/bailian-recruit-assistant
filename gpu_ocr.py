"""Prefer a discrete DirectML GPU and fall back to CPU automatically."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import uuid


def list_adapters():
    """Enumerate DXGI adapters in the order used by DirectML."""
    if sys.platform != 'win32':
        return []

    class GUID(ctypes.Structure):
        _fields_ = [('data1', wintypes.DWORD), ('data2', wintypes.WORD),
                    ('data3', wintypes.WORD), ('data4', ctypes.c_ubyte * 8)]

    class LUID(ctypes.Structure):
        _fields_ = [('low', wintypes.DWORD), ('high', wintypes.LONG)]

    class AdapterDesc(ctypes.Structure):
        _fields_ = [('description', wintypes.WCHAR * 128), ('vendor', wintypes.UINT),
                    ('device', wintypes.UINT), ('subsystem', wintypes.UINT),
                    ('revision', wintypes.UINT), ('video_memory', ctypes.c_size_t),
                    ('system_memory', ctypes.c_size_t), ('shared_memory', ctypes.c_size_t),
                    ('luid', LUID), ('flags', wintypes.UINT)]

    def invoke(pointer, index, result_type, argument_types, *arguments):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)(table[index])
        return method(pointer, *arguments)

    def release(pointer):
        invoke(pointer, 2, wintypes.ULONG, ())

    factory = ctypes.c_void_p()
    interface = GUID.from_buffer_copy(uuid.UUID('770aae78-f26f-4dba-a829-253c83d1b387').bytes_le)
    create = ctypes.WinDLL('dxgi').CreateDXGIFactory1
    create.argtypes = (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    create.restype = wintypes.LONG
    if create(ctypes.byref(interface), ctypes.byref(factory)) < 0:
        raise RuntimeError('无法枚举显卡，请检查显卡驱动')
    adapters = []
    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            status = invoke(factory, 12, wintypes.LONG,
                            (wintypes.UINT, ctypes.POINTER(ctypes.c_void_p)), index, ctypes.byref(adapter))
            if status & 0xffffffff == 0x887a0002:
                break
            if status < 0:
                raise RuntimeError('读取显卡列表失败')
            try:
                description = AdapterDesc()
                if invoke(adapter, 10, wintypes.LONG, (ctypes.POINTER(AdapterDesc),), ctypes.byref(description)) < 0:
                    raise RuntimeError('读取显卡信息失败')
                name = description.description
                adapters.append({'index': index, 'name': name, 'vendor': description.vendor,
                                 'memory': description.video_memory, 'software': bool(description.flags & 2)})
            finally:
                release(adapter)
            index += 1
    finally:
        release(factory)
    return adapters


def find_discrete_adapter(adapters=None):
    adapters = list_adapters() if adapters is None else adapters
    candidates = [adapter for adapter in adapters if not adapter['software'] and
                  (adapter['vendor'] == 0x10de or
                   (adapter['vendor'] in (0x1002, 0x8086) and adapter['memory'] >= 2 * 1024**3))]
    if not candidates:
        return None
    return max(candidates, key=lambda adapter: ('4060' in adapter['name'], adapter['memory']))


def create_ocr(force_cpu=False):
    import onnxruntime as ort
    import rapidocr_onnxruntime
    from rapidocr_onnxruntime import RapidOCR
    ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1, use_cls=False,
                   det_use_dml=False, rec_use_dml=False, cls_use_dml=False,
                   det_use_cuda=False, rec_use_cuda=False, cls_use_cuda=False)
    ocr.backend_note = ''
    if force_cpu:
        return ocr, 'CPU'
    try:
        adapter = find_discrete_adapter()
    except Exception as error:
        ocr.backend_note = str(error)
        return ocr, 'CPU · 显卡检测未成功'
    if adapter is None:
        return ocr, 'CPU · 未检测到独立显卡'
    if 'DmlExecutionProvider' not in ort.get_available_providers():
        return ocr, 'CPU · GPU 运行库不可用'
    model_dir = Path(rapidocr_onnxruntime.__file__).resolve().parent / 'models'
    options = ort.SessionOptions()
    options.enable_mem_pattern = False
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    providers = [('DmlExecutionProvider', {'device_id': str(adapter['index'])}), 'CPUExecutionProvider']

    # Keep RapidOCR's preprocessing and decoding; replace the two inference
    # sessions with explicitly configured DirectML sessions on the chosen GPU.
    sessions = []
    try:
        for engine, filename in ((ocr.text_det.infer, 'ch_PP-OCRv4_det_infer.onnx'),
                                 (ocr.text_rec.session, 'ch_PP-OCRv4_rec_infer.onnx')):
            session = ort.InferenceSession(str(model_dir / filename), sess_options=options, providers=providers)
            if session.get_providers()[0] != 'DmlExecutionProvider':
                raise RuntimeError('模型未能启用 GPU 后端')
            session.disable_fallback()
            sessions.append((engine, session))
    except Exception as error:
        ocr.backend_note = str(error)
        return ocr, 'CPU · GPU 初始化未成功'
    # Commit only after both GPU sessions are ready; otherwise keep both CPU sessions.
    for engine, session in sessions:
        engine.session = session
    return ocr, f"{adapter['name']} · DirectML"
