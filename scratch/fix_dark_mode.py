import os
import re

# Directory containing templates
TEMPLATES_DIR = '/home/yudi/dev/attendance system/backend/app/templates'

def process_class_string(class_str):
    classes = class_str.split()
    new_classes = list(classes)
    
    # We want to check for presence of dark versions
    has_dark_bg = any(c.startswith('dark:bg-') for c in classes)
    has_dark_text = any(c.startswith('dark:text-') for c in classes)
    has_dark_border = any(c.startswith('dark:border-') for c in classes)
    has_dark_hover_bg = any(c.startswith('dark:hover:bg-') for c in classes)

    # Let's map individual class replacements
    for c in classes:
        # Backgrounds
        if c == 'bg-white' and not has_dark_bg:
            new_classes.append('dark:bg-slate-900')
        elif c == 'bg-slate-50' and not has_dark_bg:
            new_classes.append('dark:bg-slate-800/50')
        elif c == 'bg-slate-100' and not has_dark_bg:
            new_classes.append('dark:bg-slate-800')
            
        # Borders
        elif c == 'border-slate-200' and not has_dark_border:
            new_classes.append('dark:border-slate-800')
        elif c == 'border-slate-300' and not has_dark_border:
            new_classes.append('dark:border-slate-700')
        elif c == 'border-slate-100' and not has_dark_border:
            new_classes.append('dark:border-slate-800/50')
            
        # Text
        elif c == 'text-slate-800' and not has_dark_text:
            new_classes.append('dark:text-slate-100')
        elif c == 'text-slate-700' and not has_dark_text:
            new_classes.append('dark:text-slate-300')
        elif c == 'text-slate-600' and not has_dark_text:
            new_classes.append('dark:text-slate-400')
        elif c == 'text-slate-505' or c == 'text-slate-500' and not has_dark_text:
            new_classes.append('dark:text-slate-400')
        elif c == 'text-slate-400' and not has_dark_text:
            new_classes.append('dark:text-slate-500')
            
        # Hover states
        elif c == 'hover:bg-slate-50' and not has_dark_hover_bg:
            new_classes.append('dark:hover:bg-slate-800/40')
            
        # Specialty badges / alerts (Indigo/Blue)
        elif c == 'bg-indigo-50/20' and not has_dark_bg:
            new_classes.append('dark:bg-indigo-950/10')
        elif c == 'bg-indigo-50' and not has_dark_bg:
            new_classes.append('dark:bg-indigo-950/30')
        elif c == 'bg-indigo-100/80' and not has_dark_bg:
            new_classes.append('dark:bg-indigo-950/50')
        elif c == 'bg-indigo-100' and not has_dark_bg:
            new_classes.append('dark:bg-indigo-950/40')
            
        elif c == 'text-indigo-900' and not has_dark_text:
            new_classes.append('dark:text-indigo-300')
        elif c == 'text-indigo-700' and not has_dark_text:
            new_classes.append('dark:text-indigo-400')
        elif c == 'text-indigo-600' and not has_dark_text:
            new_classes.append('dark:text-indigo-400')
        elif c == 'text-indigo-500' and not has_dark_text:
            new_classes.append('dark:text-indigo-400')
            
        elif c == 'border-indigo-100' and not has_dark_border:
            new_classes.append('dark:border-indigo-900/40')
        elif c == 'border-indigo-200' and not has_dark_border:
            new_classes.append('dark:border-indigo-800/60')
        elif c == 'border-indigo-300' and not has_dark_border:
            new_classes.append('dark:border-indigo-800')
        elif c == 'hover:bg-indigo-50' and not has_dark_hover_bg:
            new_classes.append('dark:hover:bg-indigo-950/30')
            
        # Emerald/Green
        elif c == 'bg-emerald-50' and not has_dark_bg:
            new_classes.append('dark:bg-emerald-950/30')
        elif c == 'text-emerald-700' and not has_dark_text:
            new_classes.append('dark:text-emerald-400')
        elif c == 'text-emerald-800' and not has_dark_text:
            new_classes.append('dark:text-emerald-400')
        elif c == 'border-emerald-300' and not has_dark_border:
            new_classes.append('dark:border-emerald-900/50')
            
        # Amber/Orange
        elif c == 'bg-amber-50/50' and not has_dark_bg:
            new_classes.append('dark:bg-amber-950/20')
        elif c == 'bg-amber-50' and not has_dark_bg:
            new_classes.append('dark:bg-amber-950/30')
        elif c == 'text-amber-700' and not has_dark_text:
            new_classes.append('dark:text-amber-400')
        elif c == 'text-amber-800' and not has_dark_text:
            new_classes.append('dark:text-amber-400')
        elif c == 'border-amber-300' and not has_dark_border:
            new_classes.append('dark:border-amber-900/50')
        elif c == 'border-amber-400' and not has_dark_border:
            new_classes.append('dark:border-amber-800/50')
            
        # Red
        elif c == 'bg-red-50/50' and not has_dark_bg:
            new_classes.append('dark:bg-red-950/20')
        elif c == 'bg-red-50' and not has_dark_bg:
            new_classes.append('dark:bg-red-950/30')
        elif c == 'text-red-700' and not has_dark_text:
            new_classes.append('dark:text-red-400')
        elif c == 'text-red-800' and not has_dark_text:
            new_classes.append('dark:text-red-400')
        elif c == 'border-red-300' and not has_dark_border:
            new_classes.append('dark:border-red-900/50')
        elif c == 'hover:bg-red-100' and not has_dark_hover_bg:
            new_classes.append('dark:hover:bg-red-900/30')

    # Deduplicate while preserving order
    seen = set()
    result = []
    for cls in new_classes:
        if cls not in seen:
            seen.add(cls)
            result.append(cls)
    return ' '.join(result)

def replace_classes(html_content):
    # Regex to find class attributes
    pattern = re.compile(r'class="([^"]*)"')
    
    def replacer(match):
        original_class_str = match.group(1)
        processed = process_class_string(original_class_str)
        return f'class="{processed}"'
        
    return pattern.sub(replacer, html_content)

def process_file(filepath):
    print(f"Processing: {filepath}")
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    updated = replace_classes(content)
    
    # Special manual adjustments
    # 1. Let's fix table dividers: tbody element divide-y
    updated = updated.replace('divide-slate-100', 'divide-slate-100 dark:divide-slate-800/50')
    # 2. Table row hover effects:
    updated = updated.replace('hover:bg-slate-50', 'hover:bg-slate-50 dark:hover:bg-slate-800/40')
    
    if updated != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(updated)
        print(f"  [UPDATED] successfully styled dark mode classes.")
    else:
        print(f"  [NO CHANGES] already optimized.")

def walk_and_process():
    for root, dirs, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if file.endswith('.html') and file != 'layout.html':  # We already did layout.html
                filepath = os.path.join(root, file)
                process_file(filepath)

if __name__ == '__main__':
    walk_and_process()
    print("Dark mode class parsing complete!")
