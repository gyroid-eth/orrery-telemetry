"""Shared payload rendering/proxy selection and read-only effect projection."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys


def template_bytes(raw, hooks_dir):
    with io.TextIOWrapper(io.BytesIO(raw),encoding='utf-8') as text:
        return text.read().replace('__AGENTSTACK_HOOKS_DIR__',hooks_dir).encode('utf-8')


def proxy_sources(source):
    for name in ('plugin','src'):
        path=source/name
        if path.is_dir():yield path,name


def proxy_ignore(directory,names):
    return shutil.ignore_patterns('__pycache__','*.pyc','*.pyo')(directory,names)


def copy_proxy(source,dest):
    for src,name in proxy_sources(source):
        dst=dest/name
        if dst.exists():shutil.rmtree(dst)
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(src,dst,ignore=proxy_ignore)


def render_file(path,hooks_dir):
    data=template_bytes(path.read_bytes(),hooks_dir)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def readonly_audit(event,args):
    if event=='open':
        mode=args[1];flags=args[2]
        if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
            raise ValueError('PLACE_ONLY_INVENTORY_WRITE_ATTEMPT')
    if event in {'os.mkdir','os.remove','os.rmdir','os.rename','os.chmod','os.chown','os.link','os.symlink','os.truncate','os.utime','os.system','os.posix_spawn','subprocess.Popen'} or event.startswith('socket.'):
        raise ValueError('PLACE_ONLY_INVENTORY_WRITE_ATTEMPT')


class Projection:
    def __init__(self,root,tier,mask,hooks_dir):
        self.root=Path(root);self.tier=tier;self.mask=mask;self.hooks_dir=hooks_dir
        self.entries={};self.directories={str(self.root)}

    def destination(self,path):
        normalized=Path(os.path.normpath(path))
        normalized.relative_to(self.root)
        return str(normalized)

    def regular(self,source,destination,preserve=False):
        destination=self.destination(destination);info=source.stat()
        if not stat.S_ISREG(info.st_mode):raise ValueError('PLACE_ONLY_INVENTORY_SOURCE_TYPE_INVALID')
        mode=stat.S_IMODE(info.st_mode)
        if not preserve:
            previous=self.entries.get(destination)
            mode=previous['mode'] if previous and previous['type']=='regular' else mode&~self.mask
        self.entries[destination]={'type':'regular','data':source.read_bytes(),'mode':mode}

    def tree(self,source,destination,proxy=False):
        self.directories.add(self.destination(destination))
        children=sorted(source.iterdir(),key=lambda p:p.name)
        ignored=proxy_ignore(str(source),[p.name for p in children]) if proxy else set()
        for child in children:
            if child.name in ignored:continue
            target=Path(destination)/child.name
            if child.is_symlink() and not proxy:
                mode=(0o777&~self.mask) if sys.platform=='darwin' else 0o777
                self.entries[self.destination(target)]={'type':'link','target':os.readlink(child),'mode':mode}
            elif child.is_dir():self.tree(child,target,proxy)
            else:self.regular(child,target,preserve=proxy)

    def command(self,args):
        if args[:2]==['mkdir','-p']:
            self.directories.update(self.destination(p) for p in args[2:]);return
        if args[:2]==['rm','-f']:
            for p in args[2:]:self.entries.pop(self.destination(p),None)
            return
        if args[:2]==['chmod','+x']:
            for p in args[2:]:self.entries[self.destination(p)]['mode']|=0o111&~self.mask
            return
        if args[0]=='child-proxy' and len(args)==3:
            for src,name in proxy_sources(Path(args[1])):self.tree(src,Path(args[2])/name,proxy=True)
            return
        if args[0]!='cp' or len(args) not in (3,4):raise ValueError('PLACE_ONLY_INVENTORY_OPERATION_INVALID')
        recursive=args[1]=='-R'
        if recursive and len(args)!=4 or not recursive and len(args)!=3:raise ValueError('PLACE_ONLY_INVENTORY_OPERATION_INVALID')
        source_text,destination=args[-2:];source=Path(source_text)
        contents=source_text.endswith('/.')
        if not contents and (destination.endswith('/') or self.destination(destination) in self.directories):destination=str(Path(destination)/source.name)
        if recursive and source.is_symlink():
            mode=(0o777&~self.mask) if sys.platform=='darwin' else 0o777
            self.entries[self.destination(destination)]={'type':'link','target':os.readlink(source),'mode':mode}
        elif recursive and source.is_dir():self.tree(source,destination)
        else:self.regular(source,destination)

    def finish(self):
        if self.tier!='dashboard-only':
            path=self.destination(Path(self.hooks_dir)/'settings.template.json')
            row=self.entries[path]
            if row['type']!='regular':raise ValueError('PLACE_ONLY_INVENTORY_TEMPLATE_TYPE_INVALID')
            row['data']=template_bytes(row['data'],self.hooks_dir);row['mode']=0o666&~self.mask
        result=[]
        for absolute,row in sorted(self.entries.items()):
            entry={'path':str(Path(absolute).relative_to(self.root)),'type':row['type'],'mode':format(row['mode'],'04o')}
            if row['type']=='regular':entry['sha256']=hashlib.sha256(row['data']).hexdigest()
            else:entry['target']=row['target']
            result.append(entry)
        return {'schema':'place-only-inventory-v1','install_dir':str(self.root),'tier':self.tier,
                'inputs':{'umask':format(self.mask,'04o'),'hooks_dir':self.hooks_dir,'platform':sys.platform},'entries':result}


def inventory(root,tier,mask,hooks_dir,stream):
    projection=Projection(root,tier,mask,hooks_dir)
    raw=stream.read(1048577)
    if len(raw)>1048576 or raw and not raw.endswith(b'\0'):raise ValueError('PLACE_ONLY_INVENTORY_STREAM_INVALID')
    fields=raw.split(b'\0')[:-1];position=0
    while position<len(fields):
        size=int(fields[position]);position+=1
        if not 1<=size<=4096 or position+size>len(fields):raise ValueError('PLACE_ONLY_INVENTORY_STREAM_INVALID')
        args=[os.fsdecode(part) for part in fields[position:position+size]];position+=size
        projection.command(args)
    return projection.finish()


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('inventory');p.add_argument('install_dir');p.add_argument('tier',choices=('default','scoped','dashboard-only'));p.add_argument('umask');p.add_argument('hooks_dir')
    p=sub.add_parser('copy-child-proxy');p.add_argument('source');p.add_argument('destination')
    p=sub.add_parser('render-template');p.add_argument('path');p.add_argument('hooks_dir')
    args=parser.parse_args()
    try:
        if args.command=='inventory':
            sys.addaudithook(readonly_audit)
            result=inventory(args.install_dir,args.tier,int(args.umask,8),args.hooks_dir,sys.stdin.buffer)
            print(json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')))
        elif args.command=='copy-child-proxy':copy_proxy(Path(args.source),Path(args.destination))
        else:render_file(Path(args.path),args.hooks_dir)
    except (OSError,ValueError,KeyError,RuntimeError):
        print('PLACE_ONLY_INVENTORY_REJECTED' if args.command=='inventory' else 'PAYLOAD_OPERATION_REJECTED',file=sys.stderr)
        return 2
    return 0


if __name__=='__main__':raise SystemExit(main())
