"""Minimal draw.io (.drawio / mxGraph XML) writer.

Produces files that diagrams.net, the draw.io desktop app and the VS Code
extension open natively. Only plain vertices, container groups and orthogonal
edges are emitted, so every generated diagram stays fully editable.

A file holds one or more pages; each page is a flat list of cells. Containers
are ordinary nodes whose id is passed as `parent` to their children; child
coordinates are relative to the parent, draw.io convention."""
import xml.etree.ElementTree as ET


# Shared style palette so all generated diagrams look consistent.
SUBSCRIPTION = ("rounded=1;whiteSpace=wrap;html=1;dashed=1;verticalAlign=top;"
                "align=left;spacingLeft=8;fillColor=#FFFFFF;strokeColor=#5C6BC0;"
                "fontSize=13;fontStyle=1;fontColor=#3949AB")
RESOURCE_GROUP = ("rounded=1;whiteSpace=wrap;html=1;verticalAlign=top;align=left;"
                  "spacingLeft=8;fillColor=#F5F5F5;strokeColor=#9E9E9E;"
                  "fontSize=12;fontStyle=1;fontColor=#424242")
CLUSTER = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#0078D4;strokeColor=#005A9E;"
           "fontColor=#FFFFFF;fontSize=12;fontStyle=1")
API_SERVER = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#DEECF9;strokeColor=#0078D4;"
              "fontColor=#004578;fontSize=11")
POOL = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#DFF6DD;strokeColor=#107C10;"
        "fontColor=#0B5A08;fontSize=11")
RESOURCE = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#F3E8FD;strokeColor=#8661C5;"
            "fontColor=#5C2E91;fontSize=11")
SUBNET = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#FFF4CE;strokeColor=#C19C00;"
          "fontColor=#6D5700;fontSize=11")
VNET = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#FFE8D9;strokeColor=#D83B01;"
        "fontColor=#A4262C;fontSize=11;fontStyle=1")
NET_ATTACH = ("rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#605E5C;"
              "fontColor=#323130;fontSize=10;dashed=1")
EDGE = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jettySize=auto;"
        "strokeColor=#605E5C;fontSize=10;fontColor=#605E5C")
EDGE_DASHED = EDGE + ";dashed=1"


class Page:
    """One draw.io page: add nodes/containers/edges, then serialize via to_xml()."""

    def __init__(self, name):
        self.name = name
        self.cells = []
        self._seq = 1

    def _next_id(self):
        self._seq += 1
        return "c%d" % self._seq

    def node(self, label, x, y, w, h, style, parent=None):
        nid = self._next_id()
        self.cells.append({"id": nid, "value": label, "style": style, "vertex": True,
                           "parent": parent or "1", "x": x, "y": y, "w": w, "h": h})
        return nid

    # A container is just a node created before its children; children pass
    # its id as `parent` and use coordinates relative to it.
    container = node

    def edge(self, source, target, label="", style=EDGE):
        eid = self._next_id()
        self.cells.append({"id": eid, "value": label, "style": style, "edge": True,
                           "parent": "1", "source": source, "target": target})
        return eid


def to_xml(pages):
    """Serialize Page objects into a complete .drawio file (one tab per page)."""
    mxfile = ET.Element("mxfile", host="app.diagrams.net", type="device")
    for i, page in enumerate(pages):
        diagram = ET.SubElement(mxfile, "diagram", id="page-%d" % i, name=page.name)
        model = ET.SubElement(diagram, "mxGraphModel", dx="1200", dy="800", grid="1",
                              gridSize="10", page="1", pageWidth="1600",
                              pageHeight="1200", math="0", shadow="0")
        root = ET.SubElement(model, "root")
        ET.SubElement(root, "mxCell", id="0")
        ET.SubElement(root, "mxCell", id="1", parent="0")
        for c in page.cells:
            attrs = {"id": c["id"], "style": c["style"], "parent": c["parent"]}
            if c.get("value"):
                attrs["value"] = str(c["value"])
            if c.get("vertex"):
                attrs["vertex"] = "1"
                cell = ET.SubElement(root, "mxCell", attrs)
                ET.SubElement(cell, "mxGeometry", x=str(c["x"]), y=str(c["y"]),
                              width=str(c["w"]), height=str(c["h"]), **{"as": "geometry"})
            else:
                attrs.update({"edge": "1", "source": c["source"], "target": c["target"]})
                cell = ET.SubElement(root, "mxCell", attrs)
                ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})
    return ET.tostring(mxfile, encoding="unicode", xml_declaration=True)


def save(pages, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(to_xml(pages))
    return path
