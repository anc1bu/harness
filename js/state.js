// Centralized state store with subscribe/notify pattern.
// All views read from getState() and mutate via setState().

const _state = {
  user: null,
  rows: [],
  columns: [],
};

const _subscribers = {};

export function getState(key) {
  return _state[key];
}

export function setState(key, value) {
  _state[key] = value;
  (_subscribers[key] || []).forEach(fn => fn(value));
}

export function subscribe(key, fn) {
  if (!_subscribers[key]) _subscribers[key] = [];
  _subscribers[key].push(fn);
}

export function unsubscribe(key, fn) {
  if (!_subscribers[key]) return;
  _subscribers[key] = _subscribers[key].filter(f => f !== fn);
}
