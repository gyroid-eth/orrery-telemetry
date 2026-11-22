"""Global markup is explicit; only shared visual styles/manifest come from legacy."""
from html.parser import HTMLParser
from pathlib import Path


class SharedHead(HTMLParser):
    """Allow only the canonical styles and named theme manifest, never handlers."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts=[]
        self.capture=None

    def handle_starttag(self, tag, attrs):
        values=dict(attrs)
        if tag=='style' and values=={'id':'agentstack-dashboard-styles'}:
            self.capture='style';self.parts.append('<style id="agentstack-dashboard-styles">')
        elif (tag=='script' and values=={'id':'agentstack-theme-axis-inventory','type':'application/json'}):
            self.capture='script';self.parts.append('<script id="agentstack-theme-axis-inventory" type="application/json">')

    def handle_data(self, data):
        if self.capture:self.parts.append(data)

    def handle_endtag(self, tag):
        if tag==self.capture:
            self.parts.append('</'+tag+'>');self.capture=None


def render(source):
    shared=SharedHead();shared.feed(source.decode('utf-8'))
    head='''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ORRERY · Agent Telemetry</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,400&amp;family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&amp;display=swap" rel="stylesheet">
'''
    shell=Path(__file__).with_name('global_shell.html').read_text()
    return (head+''.join(shared.parts)+'<link rel="stylesheet" href="theme_light.css"><link rel="stylesheet" href="mail_flow.css"><link rel="stylesheet" href="global_ui.css"></head>'+shell+'</html>').encode()
