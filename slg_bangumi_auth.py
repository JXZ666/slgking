"""Local Bangumi PAT storage protected by Windows DPAPI; never part of backups."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import tempfile

TOKEN_PAGE='https://next.bgm.tv/demo/access-token'

def validate_format(token):
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]{8,4096}={0,2}',token):
        raise ValueError('访问令牌格式无效')
    return token

class _Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]

def _crypt(raw, protect):
    if os.name!='nt': raise OSError('令牌安全存储需要Windows DPAPI')
    buf=ctypes.create_string_buffer(raw)
    source=_Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte))); output=_Blob()
    dll=ctypes.windll.crypt32
    local_free=ctypes.windll.kernel32.LocalFree
    local_free.argtypes=[ctypes.c_void_p];local_free.restype=ctypes.c_void_p
    if protect:
        success=dll.CryptProtectData(ctypes.byref(source),'SLGking Bangumi PAT',None,None,None,1,ctypes.byref(output))
    else:
        success=dll.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output))
    if not success: raise OSError('本机访问令牌加密或解密失败')
    try: return ctypes.string_at(output.data,output.size)
    finally: local_free(output.data)

class TokenStore:
    def __init__(self, directory, protect=None, unprotect=None):
        self.path=Path(directory)/'bangumi-pat.dpapi'
        self.protect=protect or (lambda raw:_crypt(raw,True))
        self.unprotect=unprotect or (lambda raw:_crypt(raw,False))
    def save(self,token):
        raw=self.protect(validate_format(token).encode('ascii'))
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.bangumi-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'wb') as output:
                output.write(raw); output.flush(); os.fsync(output.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name): os.unlink(name)
    def load(self):
        if not self.path.exists(): return ''
        try:
            if self.path.stat().st_size>16384: raise ValueError()
            return validate_format(self.unprotect(self.path.read_bytes()).decode('ascii'))
        except (OSError,ValueError,UnicodeError):
            raise OSError('本机访问令牌无法读取，请移除后重新授权') from None
    def remove(self):
        self.path.unlink(missing_ok=True)
