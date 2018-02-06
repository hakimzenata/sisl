"""
An interface routine for plotting the different systems in sisl

It merely calls the `<>.__plot__(**)` routine and returns immediately
"""

try:
    import matplotlib as mlib
    import matplotlib.pyplot as mlibplt
    import mpl_toolkits.mplot3d as mlib3d
    has_matplotlib = True
except Exception as _matplotlib_import_exception:
    mlib = NotImplementedError
    mlibplt = NotImplementedError
    mlib3d = NotImplementedError
    has_matplotlib = False

__all__ = ['plot', 'mlib', 'mlibplt', 'mlib3d']


def _plot(obj, *args, **kwargs):
    try:
        a = getattr(obj, '__plot__')
    except AttributeError:
        raise NotImplementedError("{} does not implement the __plot__ method.".format(obj.__class__.__name__))
    return a(*args, **kwargs)

if has_matplotlib:
    plot = _plot
else:
    def plot(obj, *args, **kwargs):
        raise _matplotlib_import_exception
