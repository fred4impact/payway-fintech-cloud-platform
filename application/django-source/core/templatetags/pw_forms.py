"""Design-system helpers for forms: consistent fields and flow steppers."""
from django import forms, template

register = template.Library()


@register.inclusion_tag('partials/_field.html')
def pw_field(field, label=None, size='lg', hint=None, prefix=None, show_optional=True):
    """Render a bound field with label, styled widget, help text and errors."""
    widget = field.field.widget
    kind = 'input'
    css = ['form-control']
    if isinstance(widget, (forms.CheckboxInput,)):
        kind, css = 'check', ['form-check-input']
    elif isinstance(widget, (forms.RadioSelect, forms.CheckboxSelectMultiple)):
        kind, css = 'choices', []
    elif isinstance(widget, forms.Select):
        css = ['form-select']
    elif isinstance(widget, forms.ClearableFileInput) or isinstance(widget, forms.FileInput):
        kind = 'file'
    if size == 'lg' and kind in ('input',) and not isinstance(widget, forms.Textarea):
        css.append('form-control-lg' if 'form-control' in css else 'form-select-lg')
    if size == 'lg' and css == ['form-select']:
        css.append('form-select-lg')
    if field.errors:
        css.append('is-invalid')
    existing = [c for c in widget.attrs.get('class', '').split() if c not in ('form-control', 'form-select', 'form-control-sm', 'form-control-lg')]
    attrs = {'class': ' '.join(dict.fromkeys(css + existing))}
    if field.errors:
        attrs['aria-invalid'] = 'true'
    if field.help_text or hint:
        attrs['aria-describedby'] = f'{field.auto_id}_help'
    return {
        'field': field,
        'widget_html': field.as_widget(attrs=attrs) if kind != 'choices' else field,
        'kind': kind,
        'label': label or field.label,
        'hint': hint or field.help_text,
        'prefix': prefix,
        'required': field.field.required,
        'show_optional': show_optional,
    }


@register.inclusion_tag('partials/_stepper.html')
def pw_stepper(steps, current=1, flow=False):
    """steps: 'Recipient|Amount|Review|Done'; current: 1-based index of the active step."""
    labels = [s.strip() for s in steps.split('|') if s.strip()]
    current = int(current)
    items = [{'n': i, 'label': label,
              'state': 'done' if i < current else 'current' if i == current else 'todo'}
             for i, label in enumerate(labels, start=1)]
    pct = 0 if len(labels) < 2 else round((current - 1) / (len(labels) - 1) * 100)
    return {'items': items, 'pct': pct, 'flow': flow, 'total': len(labels)}


@register.inclusion_tag('partials/_gauge.html')
def pw_gauge(done=0, progress=0, pending=0, value='', caption='', uid='g'):
    """Semicircle gauge in three segments (solid / dark / hatched), each a % of the arc."""
    segs, start = [], 0.0
    gap = 6  # arc units between rounded segments (pathLength = 100)
    for kind, pct in (('done', done), ('progress', progress), ('pending', pending)):
        pct = float(pct or 0)
        if pct <= 0:
            continue
        inset = gap / 2 if pct > gap else 0
        segs.append({'kind': kind, 'len': max(pct - 2 * inset, 0.5), 'offset': -(start + inset)})
        start += pct
    return {'segs': segs, 'value': value, 'caption': caption, 'uid': uid}
