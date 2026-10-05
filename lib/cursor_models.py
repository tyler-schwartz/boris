"""Resolve Cursor model identifiers against its CLI's live model catalog."""
import re
import subprocess


class ModelError(ValueError):
    pass


def catalog(binary='agent'):
    try:
        result = subprocess.run([binary, 'models'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ModelError(f'cannot read Cursor CLI models: {exc}')
    if result.returncode:
        raise ModelError(result.stderr.strip() or 'Cursor CLI could not list models')
    models = {}
    for line in result.stdout.splitlines():
        line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
        if ' - ' not in line:
            continue
        identifier, label = line.split(' - ', 1)
        identifier = identifier.strip()
        if re.fullmatch(r'[a-zA-Z0-9_.-]+', identifier):
            models[identifier] = label.strip()
    if not models:
        raise ModelError('Cursor CLI returned no model identifiers')
    return models


def key(name):
    # Some calling threads add the provider to the UI's model name.
    name = re.sub(r'^cursor[\s-]+', '', name.strip(), flags=re.I)
    return re.sub(r'[^a-z0-9]', '', name.casefold())


def resolve_model(model, parameters=(), models=None):
    models = catalog() if models is None else models
    base, bracket, overrides = model.strip().partition('[')
    if bracket:
        if base not in models or not overrides.endswith(']'):
            raise ModelError(f'unknown Cursor CLI model: {model}')
        if parameters:
            raise ModelError('pass Cursor model settings as either bracket overrides or parameters')
        return model.strip()  # Preserve explicit CLI bracket settings exactly.
    effort = None
    fast = None
    for param in parameters:
        if param.get('id') in {'reasoning_effort', 'effort'}:
            effort = str(param.get('value')).lower()
        if param.get('id') == 'fast':
            fast = str(param.get('value')).lower() == 'true'
    if base in models:
        identifier = base
    else:
        matches = [identifier for identifier, label in models.items() if key(label) == key(base)]
        if not matches and effort is not None and re.fullmatch(r'[a-zA-Z0-9_.-]+', base):
            levels = ['xhigh', 'extra-high'] if effort in {'xhigh', 'extra-high'} else [effort]
            candidates = ['-'.join(part for part in [family, level, 'fast' if fast else None] if part)
                          for family in [base, 'cursor-' + base] for level in levels]
            available = list(dict.fromkeys(candidate for candidate in candidates if candidate in models))
            if len(available) == 1:
                return available[0]
        if len(matches) != 1:
            reason = 'ambiguous' if matches else 'unknown'
            raise ModelError(f'{reason} Cursor model {model!r}; use an exact identifier from `agent models` '
                             'or omit --model to read the calling Cursor conversation')
        identifier = matches[0]
    if effort is None and fast is None:
        return identifier
    current_fast = identifier.endswith('-fast')
    family = identifier.removesuffix('-fast')
    current = re.search(r'-(extra-high|xhigh|high|medium|low|max|minimal|none)$', family)
    # Unlisted family IDs may be selected in the IDE with separate parameters.
    if current:
        family = family[:current.start()]
    efforts = [effort] if effort is not None else [current[1] if current else None]
    if effort in {'xhigh', 'extra-high'}:
        efforts = ['xhigh', 'extra-high']
    use_fast = current_fast if fast is None else fast
    for level in efforts:
        candidate = '-'.join(part for part in [family, level, 'fast' if use_fast else None] if part)
        if candidate in models:
            return candidate
    raise ModelError(f'Cursor model {model!r} has no CLI variant with effort={effort} fast={use_fast}; '
                     'refusing to drop the thread settings')
