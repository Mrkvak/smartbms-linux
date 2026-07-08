import struct, os, lz4.block
blob=open('extracted/assemblies/assemblies.blob','rb').read()
# manifest: map blob idx -> name
names={}
for line in open('extracted/assemblies/assemblies.manifest').read().splitlines()[1:]:
    p=line.split()
    if len(p)>=5:
        names[int(p[3])]=p[4]
lec=struct.unpack('<I',blob[8:12])[0]
os.makedirs('dlls',exist_ok=True)
off=0x14
for i in range(lec):
    do,ds,ddo,dds,cdo,cds=struct.unpack('<IIIIII',blob[off:off+24]); off+=24
    data=blob[do:do+ds]
    name=names.get(i,f'idx{i}')
    if data[:4]==b'XALZ':
        ulen=struct.unpack('<I',data[8:12])[0]
        out=lz4.block.decompress(data[12:],uncompressed_size=ulen)
    else:
        out=data
    open(f'dlls/{name}.dll','wb').write(out)
print('extracted',lec,'assemblies')
for n in ['123Connection','123ConnectionBLEAndroid','123SmartBMS','SmartBMS','123Helpers']:
    print(n, os.path.getsize(f'dlls/{n}.dll'))
