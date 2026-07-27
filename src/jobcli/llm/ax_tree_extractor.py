"""Extract Accessibility Tree (AXTree) for efficient LLM processing."""

from typing import Any

from playwright.sync_api import Page
from pydantic import BaseModel, Field


class AccessibilityNode(BaseModel):
    """A node in the accessibility tree."""

    role: str
    name: str = ""
    value: str = ""
    description: str = ""
    focused: bool = False
    focusable: bool = False
    editable: bool = False
    required: bool = False
    disabled: bool = False
    expanded: bool | None = None
    level: int | None = None
    multiselectable: bool = False
    readonly: bool = False
    selected: bool | None = None
    checked: bool | None = None
    pressed: bool | None = None
    invalid: str = ""
    orientation: str = ""
    autocomplete: str = ""
    children: list["AccessibilityNode"] = Field(default_factory=list)


class AccessibilityTree(BaseModel):
    """Complete accessibility tree."""

    url: str
    title: str
    root: AccessibilityNode
    interactive_nodes: list[dict[str, Any]] = Field(default_factory=list)
    form_fields: list[dict[str, Any]] = Field(default_factory=list)
    dropdown_fields: list[dict[str, Any]] = Field(default_factory=list)
    buttons: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)


class AccessibilityTreeExtractor:
    """Extract accessibility tree from page."""

    def __init__(self, page: Page) -> None:
        """Initialize extractor."""
        self.page = page

    def extract(self) -> AccessibilityTree:
        """Extract full accessibility tree using aria_snapshot (primary) or CDP (fallback)."""
        url = self.page.url
        title = self.page.title()

        snapshot = None
        raw_aria = ""

        # Primary: use Playwright's aria_snapshot — reliable across all versions
        try:
            raw_aria = self.page.locator("body").aria_snapshot()
        except Exception:
            pass

        # Also capture any ATS iframe content and append it. Patterns are
        # substrings matched against the iframe URL — keep them as the actual
        # host suffix used by the live ATS (Ashby uses ``ashbyhq.com``, not
        # ``ashby.com``; that earlier typo dropped any embedded Ashby form
        # right off the LLM's view).
        ats_patterns = [
            "greenhouse.io",
            "lever.co",
            "workday.com",
            "icims.com",
            "ashbyhq.com",
            "taleo.net",
            "smartrecruiters.com",
            "myworkdayjobs.com",
            "jobvite.com",
            "successfactors.com",
            "oraclecloud.com",
            "bamboohr.com",
            "rippling.com",
            "teamtailor.com",
            "workable.com",
        ]
        for frame in self.page.frames:
            frame_url = frame.url or ""
            if any(p in frame_url for p in ats_patterns):
                try:
                    iframe_aria = frame.locator("body").aria_snapshot()
                    if iframe_aria:
                        raw_aria += f"\n\n## Application Form (iframe: {frame_url[:60]}):\n{iframe_aria}"
                except Exception:
                    pass

        if raw_aria:
            snapshot = self._parse_aria_text(raw_aria)

        # Fallback: CDP
        if not snapshot:
            try:
                cdp = self.page.context.new_cdp_session(self.page)
                result = cdp.send("Accessibility.getFullAXTree")
                cdp.detach()
                raw_nodes = result.get("nodes", [])
                if raw_nodes:
                    snapshot = self._cdp_to_snapshot(raw_nodes)
            except Exception:
                pass

        if not snapshot:
            return AccessibilityTree(
                url=url,
                title=title,
                root=AccessibilityNode(role="WebArea", name=title),
            )

        # Parse snapshot into our model
        root = self._parse_node(snapshot)

        # Extract specific elements for LLM
        interactive_nodes = self._extract_interactive_nodes(snapshot)
        form_fields = self._extract_form_fields(snapshot)
        buttons = self._extract_buttons(snapshot)
        links = self._extract_links(snapshot)
        dropdowns = self._detect_dropdown_fields()

        tree = AccessibilityTree(
            url=url,
            title=title,
            root=root,
            interactive_nodes=interactive_nodes,
            form_fields=form_fields,
            dropdown_fields=dropdowns,
            buttons=buttons,
            links=links,
        )

        # Attach the raw aria text for direct LLM consumption
        tree._raw_aria = raw_aria  # type: ignore[attr-defined]
        return tree

    def _cdp_to_snapshot(self, raw_nodes: list[dict]) -> dict[str, Any] | None:
        """Convert flat CDP AXTree node list into a nested snapshot dict."""
        if not raw_nodes:
            return None

        node_map: dict[str, dict] = {}
        for node in raw_nodes:
            node_id = node.get("nodeId", "")
            role_obj = node.get("role", {})
            name_obj = node.get("name", {})
            value_obj = node.get("value", {})
            desc_obj = node.get("description", {})

            props = {}
            for prop in node.get("properties", []):
                props[prop.get("name", "")] = prop.get("value", {}).get("value")

            parsed = {
                "role": role_obj.get("value", "none"),
                "name": name_obj.get("value", ""),
                "value": value_obj.get("value", "") if value_obj else "",
                "description": desc_obj.get("value", "") if desc_obj else "",
                "focused": props.get("focused", False),
                "focusable": props.get("focusable", False),
                "required": props.get("required", False),
                "disabled": props.get("disabled", False),
                "readonly": props.get("readonly", False),
                "invalid": props.get("invalid", ""),
                "editable": props.get("editable", ""),
                "children": [],
            }
            node_map[node_id] = parsed

            # Wire children
            child_ids = node.get("childIds", [])
            for cid in child_ids:
                if cid in node_map:
                    parsed["children"].append(node_map[cid])

        # Second pass: wire children that were created after their parent
        for node in raw_nodes:
            node_id = node.get("nodeId", "")
            parent = node_map.get(node_id)
            if not parent:
                continue
            for cid in node.get("childIds", []):
                child = node_map.get(cid)
                if child and child not in parent["children"]:
                    parent["children"].append(child)

        # Root is the first node
        root_id = raw_nodes[0].get("nodeId", "")
        return node_map.get(root_id)

    def _aria_fallback(self) -> dict[str, Any] | None:
        """Fallback: use Playwright's aria_snapshot() to build a minimal tree."""
        try:
            aria_text = self.page.locator("body").aria_snapshot()
            if not aria_text:
                return None

            # Parse the YAML-like aria snapshot into our dict format
            return self._parse_aria_text(aria_text)
        except Exception:
            return None

    def _parse_aria_text(self, text: str) -> dict[str, Any]:
        """Parse aria_snapshot text into a hierarchical snapshot dict.
        
        The snapshot format is YAML-like:
        - role "Name" [prop=val]
          - child_role "Child"
        """
        lines = text.strip().split("\n")
        if not lines:
            return {"role": "WebArea", "name": "", "children": []}

        root: dict[str, Any] = {"role": "WebArea", "name": "", "children": []}
        stack = [( -1, root )]  # Initial depth and parent node

        import re
        # Regex to match: role "name" [attributes]
        line_pattern = re.compile(r'^(\s*)-?\s*(\w+)\s*(?:"([^"]*)")?\s*(?:\[(.*)\])?')

        for line in lines:
            if not line.strip():
                continue
            
            match = line_pattern.match(line)
            if not match:
                continue
            
            indent, role, name, attrs_str = match.groups()
            depth = len(indent)
            
            # Normalize name: strip common markers like '*' or ':' and extra whitespace
            clean_name = name or ""
            clean_name = clean_name.replace("*", "").strip()
            if clean_name.endswith(":"): clean_name = clean_name[:-1].strip()

            node: dict[str, Any] = {
                "role": role,
                "name": clean_name,
                "children": [],
            }
            
            # Parse attributes: [value="Sai", disabled]
            if attrs_str:
                # Handle value="Sai"
                val_match = re.search(r'value="([^"]*)"', attrs_str)
                if val_match:
                    node["value"] = val_match.group(1)
                
                # Handle other flags
                if "disabled" in attrs_str: node["disabled"] = True
                if "checked" in attrs_str: node["checked"] = True
                if "pressed" in attrs_str: node["pressed"] = True
                if "required" in attrs_str: node["required"] = True
                
                # Handle numeric levels
                lvl_match = re.search(r'level=(\d+)', attrs_str)
                if lvl_match:
                    node["level"] = int(lvl_match.group(1))
            
            # If the node has children and no name, it might have a text name in a child
            # (common for custom buttons/dropdowns)
            if not node["name"] and role in ["button", "combobox"]:
                # We can't do much here as children aren't processed yet,
                # but we'll handle this in the flattening/collection phase.
                pass

            # Pop stack until we find the parent (one level up)
            while stack and stack[-1][0] >= depth:
                stack.pop()
            
            if stack:
                stack[-1][1]["children"].append(node)
            
            stack.append((depth, node))

        return root

    def _parse_node(self, node: dict[str, Any]) -> AccessibilityNode:
        """Parse accessibility node."""
        children = []
        if "children" in node:
            children = [self._parse_node(child) for child in node["children"]]

        return AccessibilityNode(
            role=node.get("role", ""),
            name=node.get("name", ""),
            value=node.get("value", ""),
            description=node.get("description", ""),
            focused=node.get("focused", False),
            focusable=node.get("focusable", False),
            editable=node.get("editable", False) == "plaintext"
            or node.get("editable", False) == "richtext",
            required=node.get("required", False),
            disabled=node.get("disabled", False),
            expanded=node.get("expanded"),
            level=node.get("level"),
            multiselectable=node.get("multiselectable", False),
            readonly=node.get("readonly", False),
            selected=node.get("selected"),
            checked=node.get("checked"),
            pressed=node.get("pressed"),
            invalid=node.get("invalid", ""),
            orientation=node.get("orientation", ""),
            autocomplete=node.get("autocomplete", ""),
            children=children,
        )

    def _extract_interactive_nodes(self, node: dict[str, Any], path: str = "") -> list[dict[str, Any]]:
        """Extract interactive nodes from tree."""
        nodes = []

        role = node.get("role", "")
        interactive_roles = [
            "button",
            "link",
            "textbox",
            "searchbox",
            "combobox",
            "checkbox",
            "radio",
            "menuitem",
            "tab",
            "switch",
        ]

        if role in interactive_roles:
            nodes.append(
                {
                    "role": role,
                    "name": node.get("name", ""),
                    "value": node.get("value", ""),
                    "description": node.get("description", ""),
                    "focusable": node.get("focusable", False),
                    "disabled": node.get("disabled", False),
                    "path": path,
                }
            )

        # Recursively process children
        for i, child in enumerate(node.get("children", [])):
            child_path = f"{path}/{i}" if path else str(i)
            nodes.extend(self._extract_interactive_nodes(child, child_path))

        return nodes

    def _extract_form_fields(self, node: dict[str, Any], parent_name: str = "") -> list[dict[str, Any]]:
        """Extract form field nodes.
        
        Enhanced to handle:
        - Education sections (often in repeating groups)
        - Experience sections (dynamic forms)
        - Custom dropdowns and comboboxes
        - Nested form structures
        """
        fields = []

        role = node.get("role", "")
        name = node.get("name", "")
        
        # Build full label by combining parent context with current name
        # This helps with nested fields like "Education[0].School"
        full_label = f"{parent_name} {name}".strip() if parent_name and name else (name or "")

        # Expand roles to include all typical form fields + custom buttons used as dropdowns
        field_roles = [
            "textbox", "searchbox", "combobox", "spinbutton", 
            "checkbox", "radio", "listbox", "switch", "menuitemradio",
            "button", "group", "radiogroup"  # Groups often contain form fields
        ]

        if role in field_roles:
            # Handle checkboxes and radios: use 'checked' as value if no value exists
            val = node.get("value", "")
            if not val and node.get("checked"):
                val = "on" if node.get("checked") is True else (node.get("checked") if node.get("checked") != "mixed" else "mixed")
            
            # For comboboxes or buttons with no value, try to find a child that might represent the selected text
            if role in ["combobox", "button", "listbox"] and not val:
                for child in node.get("children", []):
                    child_role = child.get("role", "")
                    # Look for text content in various forms
                    if child_role in ["text", "StaticText", "plaintext", "paragraph"]:
                        child_name = child.get("name", "")
                        if child_name and child_name.strip():
                            val = child_name.strip()
                            break
                    # Also check for nested elements with text
                    if child.get("name") and not child.get("children"):
                        val = child.get("name")
                        break

            # Extract additional attributes for better field identification
            field_data = {
                "role": role,
                "name": full_label,  # Use full label for better identification
                "value": val or "",
                "placeholder": node.get("placeholder", ""),
                "required": node.get("required", False),
                "readonly": node.get("readonly", False),
                "invalid": node.get("invalid", ""),
                "autocomplete": node.get("autocomplete", ""),
                "checked": node.get("checked", False),
                "parent_context": parent_name if parent_name else None,
            }
            
            # Only add if it has a meaningful name (avoid anonymous containers)
            if full_label or role in ["textbox", "searchbox", "combobox", "checkbox", "radio", "listbox"]:
                fields.append(field_data)

        # Process children recursively
        # For groups/radiogroups, pass the group name as parent context
        new_parent = full_label if role in ["group", "radiogroup", "listitem", "row"] and full_label else parent_name
        
        for child in node.get("children", []):
            fields.extend(self._extract_form_fields(child, new_parent))

        return fields

    def _extract_buttons(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract button nodes."""
        buttons = []

        role = node.get("role", "")
        if role == "button":
            buttons.append(
                {
                    "name": node.get("name", ""),
                    "description": node.get("description", ""),
                    "pressed": node.get("pressed"),
                    "disabled": node.get("disabled", False),
                }
            )

        # Recursively process children
        for child in node.get("children", []):
            buttons.extend(self._extract_buttons(child))

        return buttons

    def _extract_links(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract link nodes."""
        links = []

        role = node.get("role", "")
        if role == "link":
            links.append(
                {
                    "name": node.get("name", ""),
                    "description": node.get("description", ""),
                    "url": node.get("url", ""),
                }
            )

        # Recursively process children
        for child in node.get("children", []):
            links.extend(self._extract_links(child))

        return links

    def _detect_dropdown_fields(self) -> list[dict[str, Any]]:
        """Find all dropdown/select fields and pre-extract their options from the DOM.

        For native ``<select>`` elements, options are read directly.
        For custom ARIA comboboxes (Greenhouse react-select, etc.), options are
        extracted via JS without opening the dropdown — react-select renders a
        hidden ``<div role='listbox'>`` or ``<div role='option'>`` tree that is
        accessible even when the dropdown is closed.
        """
        dropdowns = []

        frames_to_check = [self.page.main_frame] + self.page.frames

        for frame in frames_to_check:
            try:
                # 1. Native <select> elements — always fully readable
                for sel in frame.locator("select").all():
                    try:
                        label = self._find_label_for_element(sel, frame)
                        options = [
                            t.strip()
                            for opt in sel.locator("option").all()
                            if (t := opt.text_content() or "").strip()
                        ]
                        if options:
                            dropdowns.append({
                                "label": label,
                                "type": "native_select",
                                "options": options,
                            })
                    except Exception:
                        pass

                # 2. Custom ARIA comboboxes — use JS to find hidden option elements
                #    react-select keeps options in a hidden <div role='listbox'> or
                #    in aria-owns'd element even when the dropdown is closed.
                try:
                    custom_opts: list[dict] = frame.evaluate(r"""() => {
                        const results = [];
                        const seen = new Set();

                        // ── helper: get label text for a combobox ──────────────
                        function getLabel(cb) {
                            // aria-labelledby
                            const lbId = cb.getAttribute('aria-labelledby');
                            if (lbId) {
                                const el = document.getElementById(lbId);
                                if (el) return el.textContent.trim();
                            }
                            // aria-label
                            const al = cb.getAttribute('aria-label');
                            if (al) return al.trim();
                            // ancestor label element
                            const lbl = cb.closest('[data-testid], .field, .form-group, .application-question');
                            if (lbl) {
                                const txt = lbl.querySelector('label, .label, legend, p, span');
                                if (txt) return txt.textContent.trim();
                            }
                            // previous sibling text
                            let prev = cb.previousElementSibling;
                            while (prev) {
                                const t = prev.textContent.trim();
                                if (t.length > 3) return t;
                                prev = prev.previousElementSibling;
                            }
                            return '';
                        }

                        // ── helper: extract options for a combobox ─────────────
                        function getOptions(cb) {
                            const opts = new Set();
                            // aria-owns: react-select appends the listbox elsewhere in DOM
                            const owns = cb.getAttribute('aria-owns') || cb.getAttribute('aria-controls');
                            if (owns) {
                                for (const id of owns.split(/\s+/)) {
                                    const lb = document.getElementById(id);
                                    if (lb) {
                                        lb.querySelectorAll('[role="option"]').forEach(o => {
                                            const t = o.textContent.trim();
                                            if (t) opts.add(t);
                                        });
                                    }
                                }
                            }
                            // Sibling or descendant listbox
                            const container = cb.closest('.select__container, [class*="select"], [class*="Select"]') || cb.parentElement;
                            if (container) {
                                container.querySelectorAll('[role="option"]').forEach(o => {
                                    const t = o.textContent.trim();
                                    if (t) opts.add(t);
                                });
                                // react-select hidden menu
                                container.querySelectorAll('.select__option, .react-select__option, [class*="option"]').forEach(o => {
                                    const t = o.textContent.trim();
                                    if (t) opts.add(t);
                                });
                            }
                            return [...opts];
                        }

                        // Walk all comboboxes and listboxes
                        document.querySelectorAll('[role="combobox"], [role="listbox"]').forEach(cb => {
                            const label = getLabel(cb);
                            if (!label || seen.has(label)) return;
                            seen.add(label);
                            const options = getOptions(cb);
                            results.push({ label, options });
                        });
                        return results;
                    }""")
                    for item in (custom_opts or []):
                        dropdowns.append({
                            "label": item.get("label", ""),
                            "type": "custom_dropdown",
                            "options": item.get("options", []),
                        })
                except Exception:
                    pass

            except Exception:
                continue

        return dropdowns

    def _find_label_for_element(self, element, frame) -> str:
        """Attempt to find the associated label text for an element."""
        try:
            # Try aria-label first
            aria_label = element.get_attribute("aria-label")
            if aria_label:
                return aria_label.strip()
                
            # Try aria-labelledby
            aria_labelledby = element.get_attribute("aria-labelledby")
            if aria_labelledby:
                label_el = frame.locator(f"#{aria_labelledby}").first
                if label_el:
                    text = label_el.text_content()
                    if text:
                        return text.strip()
            
            # Try id -> associated <label for="id">
            el_id = element.get_attribute("id")
            if el_id:
                label_el = frame.locator(f"label[for='{el_id}']").first
                if label_el:
                    text = label_el.text_content()
                    if text:
                        return text.strip()
            
            # Try closest <label> ancestor
            # Playwright doesn't have a direct 'closest' but we can evaluate it
            text = element.evaluate("(el) => { const label = el.closest('label'); return label ? label.textContent : ''; }")
            if text and text.strip():
                return text.strip()
                
            # Try preceding text element
            text = element.evaluate("""(el) => { 
                let prev = el.previousElementSibling;
                while (prev && prev.tagName !== 'LABEL' && !prev.textContent.trim()) {
                    prev = prev.previousElementSibling;
                }
                return prev ? prev.textContent : '';
            }""")
            if text and text.strip():
                return text.strip()
                
        except Exception:
            pass
            
        return ""

    def extract_summary(self) -> dict[str, Any]:
        """Extract a summary optimized for LLM token efficiency."""
        tree = self.extract()

        # Return only the most relevant information
        return {
            "url": tree.url,
            "title": tree.title,
            "buttons": tree.buttons[:10],  # Top 10 buttons
            "form_fields": tree.form_fields[:15],  # Top 15 fields
            "links": [link for link in tree.links if "apply" in link["name"].lower()][:5],
            "interactive_count": len(tree.interactive_nodes),
        }
