from sympy import init_printing
init_printing()

import numpy as np
import sympy as sy
import matplotlib.pyplot as plt

print(f"numpy version: {np.__version__}")
print(f"sympy version: {sy.__version__}")

# Define the simple functions in terms
# of the location parameter
# We define the distributions according to
# the scipy.stats module
x, y, c = sy.symbols("x y c", real=True)
n, N = sy.symbols("n N", integer=True, finite=True)

# For the methfessel-paxton distribution
hermite = sy.functions.special.polynomials.hermite


class Distribution:
    def __init__(self, name, pdf=None, cdf=None, sf=None, entropy=None):
        self.name = name
        self.pdf = pdf
        self.cdf = cdf
        self.sf = sf
        self.entropy = entropy

    def __eq__(self, name):
        return self.name == name


distributions = [
    Distribution("fd",
                 pdf=(1-1/(sy.exp(x)+1)).diff(x),
                 sf=1/(sy.exp(x)+1)),
    Distribution("mp",
                 pdf=sy.exp(-x**2)/sy.sqrt(sy.pi)*sy.Sum(hermite(2*n, x), (n, 0, N)),
                 cdf=1/sy.sqrt(sy.pi)*sy.Sum(
                     (sy.exp(-x**2)*hermite(2*n, x))
                      .integrate(x)
                      .doit(simplify=True)
                      .expand()
                      .simplify()
                      , (n, 0, N)),
                 entropy=-1/sy.sqrt(sy.pi)*sy.Sum(
                     (sy.exp(-x**2)*hermite(2*n, x) * x)
                     .integrate((x, -sy.oo, y)).subs(y, x)
                     .doit(simplify=True)
                     .expand()
                     .simplify()
                     , (n, 0, N))),
    Distribution("gaussian",
                 pdf=sy.exp(-x**2/2)/sy.sqrt(2*sy.pi)),
    Distribution("cauchy",
                 pdf=1/(sy.pi*(1+x**2))),
    Distribution("cold",
                 pdf=1/sy.sqrt(sy.pi)*sy.exp(-(-x-1/sy.sqrt(2))**2)*(2+sy.sqrt(2)*x))
]


# Define plots
fig, axs = plt.subplots(3, 1)
E = np.linspace(-10, 10, 1001)
axs[0].set_title("PDF")
axs[1].set_title("theta|sf")
axs[2].set_title("entropy")


for dist in distributions:
    print(f"\nProcessing {dist.name}")

    # First fill the values
    if dist.pdf is None:
        dist.pdf = dist.cdf.diff(x)

    norm = sy.integrate(dist.pdf.subs(N, 0), (x, -sy.oo, sy.oo)).evalf()
    # Ensure normalization for consistency
    # For utilization in the scipy.stats module it should have normalization 1
    assert norm == 1
    print(f"  pdf|delta = {dist.pdf}")

    
    if dist.cdf is None:
        dist.cdf = sy.integrate(dist.pdf.expand(), x).doit(simplify=True).simplify()
    # Ensure that the cdf is 0 at -inf
    # The CDF is zero @ - inf
    # The CDF is  1   @ + inf
    cneg = dist.cdf.subs(x, -sy.oo)
    dist.cdf = (dist.cdf - cneg).expand().simplify()

    print(f"        cdf = {dist.cdf}")

    # plot it...
    func = dist.pdf.subs(N, 0).expand().simplify()
    func = sy.lambdify(x, func, 'numpy')
    if dist.name == 'mp':
        # we cannot convert hermite polynomials to numpy functions
        pass
        #func = lambda x: np.exp(-x**2)/np.pi**0.5
    axs[0].plot(E, func(E), label=dist.name)
    

    if dist.sf is None:
        dist.sf = (1 - dist.cdf).expand().simplify()
    print(f"   sf|theta = {dist.sf}")

    func = dist.sf.subs(N, 0).expand().simplify()
    func = sy.lambdify(x, func, 'numpy')
    try:
        # cold function may fail
        axs[1].plot(E, [func(e) for e in E], label=dist.name)
    except: pass

    if dist.entropy is None:
        dist.entropy = -(dist.pdf*x).integrate((x, -sy.oo, x)).doit(simplify=True).simplify()
    print(f"    entropy = {dist.entropy}")

    func = dist.entropy.subs(N, 0).expand().simplify()
    func = sy.lambdify(x, func, 'numpy')
    try:
        # cold function may fail
        axs[2].plot(E, [func(e) for e in E], label=dist.name)
    except: pass


ifd = distributions.index("fd")
fd = distributions[ifd]
# Check that it finds the same entropy
fd_enpy = -(fd.sf * sy.log(fd.sf) + (1-fd.sf)*sy.log(1-fd.sf))
assert (fd_enpy - fd.entropy).simplify() == 0.


axs[0].legend()
axs[1].legend()
axs[2].legend()
plt.show()    
