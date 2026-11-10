#!/usr/bin/env python3
"""Regenerate bundled phone icons: python3 scripts/mobile_pwa_icons.py (rsvg-convert required)."""
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
SVG='http://www.w3.org/2000/svg'


def main():
    source=ET.parse(ROOT/'dashboard/assets/orbit.svg').getroot()
    background=source.find('{'+SVG+'}rect').attrib['fill']
    output=ROOT/'dashboard/mobile/icons'
    output.mkdir(parents=True,exist_ok=True)
    for filename,size,scale in [('orbit-192.png',192,1),('orbit-512.png',512,1),
                                 ('orbit-maskable-512.png',512,.68),('apple-touch-icon.png',180,1)]:
        canvas=ET.Element('{'+SVG+'}svg',{'viewBox':'0 0 128 128'})
        ET.SubElement(canvas,'{'+SVG+'}rect',{'width':'128','height':'128','fill':background})
        inset=64*(1-scale)
        group=ET.SubElement(canvas,'{'+SVG+'}g',{'transform':f'translate({inset} {inset}) scale({scale})'})
        group.extend(list(source))
        data=subprocess.run(['rsvg-convert','-w',str(size),'-h',str(size)],input=ET.tostring(canvas),
                            check=True,capture_output=True).stdout
        (output/filename).write_bytes(data)


if __name__=='__main__':main()
