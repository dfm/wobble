import numpy as np
import matplotlib.pyplot as plt
import matplotlib
from matplotlib import animation
from tqdm import tqdm
import sys
import h5py
import copy
import pickle
import tensorflow as tf
T = tf.float64
import pdb

from .utils import fit_continuum, bin_data
from .interp import interp

speed_of_light = 2.99792458e8   # m/s
DATA_NP_ATTRS = ['N', 'R', 'origin_file', 'orders', 'dates', 'bervs', 'drifts', 'airms', 'pipeline_rvs', 'epoch_mask']
DATA_TF_ATTRS = ['xs', 'ys', 'ivars']
MODEL_ATTRS = ['component_names'] # not actually used but defined for completeness
COMPONENT_NP_ATTRS = ['K', 'rvs_fixed', 'template_exists', 'learning_rate_rvs', 'learning_rate_template', 'learning_rate_basis', 
                      'L1_template', 'L2_template', 'L1_basis_vectors', 'L2_basis_vectors', 'L2_basis_weights',
                      'time_rvs', 'order_rvs', 'order_sigmas']
COMPONENT_TF_ATTRS = ['rvs_block', 'ivars_block', 'template_xs', 'template_ys', 'basis_vectors', 'basis_weights']

__all__ = ["get_session", "doppler", "Data", "Model", "History", "Results", "optimize_order", "optimize_orders"]

def get_session():
  """Get the globally defined TensorFlow session.
  If the session is not already defined, then the function will create
  a global session.
  Returns:
    _SESSION: tf.Session.
  (Code from edward package.)
  """
  global _SESSION
  if tf.get_default_session() is None:
    _SESSION = tf.InteractiveSession()
    
  else:
    _SESSION = tf.get_default_session()

  save_stderr = sys.stderr
  return _SESSION

def doppler(v):
    frac = (1. - v/speed_of_light) / (1. + v/speed_of_light)
    return tf.sqrt(frac)

class Data(object):
    """
    The data object: contains the spectra and associated data.
    """
    def __init__(self, filename, filepath='../data/', 
                    N = 0, orders = [30], min_flux = 1., tensors=True,
                    mask_epochs = None):
        self.R = len(orders) # number of orders to be analyzed
        self.orders = orders
        self.origin_file = filepath+filename
        with h5py.File(self.origin_file) as f:
            if N < 1:
                self.N = len(f['dates']) # all epochs
            else:
                self.N = N
            self.ys = [f['data'][i][:self.N,:] for i in orders]
            self.xs = [np.log(f['xs'][i][:self.N,:]) for i in orders]
            self.ivars = [f['ivars'][i][:self.N,:] for i in orders]
            self.pipeline_rvs = np.copy(f['pipeline_rvs'])[:self.N] * -1.
            self.dates = np.copy(f['dates'])[:self.N]
            self.bervs = np.copy(f['bervs'])[:self.N] * -1.
            self.drifts = np.copy(f['drifts'])[:self.N]
            self.airms = np.copy(f['airms'])[:self.N]
            
        # mask out bad pixels:
        for r in range(self.R):
            bad = np.where(self.ys[r] < min_flux)
            self.ys[r][bad] = min_flux
            self.ivars[r][bad] = 0.
            
        # mask out bad epochs:
        self.epoch_mask = [True for n in range(self.N)]
        if mask_epochs is not None:
            for n in mask_epochs:
                self.epoch_mask[n] = False

        # log and normalize:
        self.ys = np.log(self.ys) 
        self.continuum_normalize() 
        
        # convert to tensors
        if tensors:
            self.ys = [tf.constant(y, dtype=T) for y in self.ys]
            self.xs = [tf.constant(x, dtype=T) for x in self.xs]
            self.ivars = [tf.constant(i, dtype=T) for i in self.ivars]
        
    def continuum_normalize(self):
        for r in range(self.R):
            for n in range(self.N):
                self.ys[r][n] -= fit_continuum(self.xs[r][n], self.ys[r][n], self.ivars[r][n])
        
                
class Model(object):
    """
    Keeps track of all components in the model.
    """
    def __init__(self, data):
        self.components = []
        self.component_names = []
        self.data = data
        
    def __str__(self):
        string = 'Model consisting of the following components: '
        for c in self.components:
            string += '\n{0}: '.format(c.name)
            if c.rvs_fixed:
                string += 'RVs fixed; '
            else:
                string += 'RVs variable; '
            string += '{0} variable basis components'.format(c.K)
        return string
        
    def synthesize(self, r):
        synth = tf.zeros_like(self.data.xs[r])
        for c in self.components:
            synth += c.synthesize(r)
        return synth
        
    def add_star(self, name, rvs_fixed=False, variable_bases=0):
        if np.isin(name, self.component_names):
            print("The model already has a component named {0}. Try something else!".format(name))
            return
        c = Star(name, self.data, rvs_fixed=rvs_fixed, variable_bases=variable_bases)
        self.components.append(c)
        self.component_names.append(name)
        
    def add_telluric(self, name, rvs_fixed=True, variable_bases=0):
        if np.isin(name, self.component_names):
            print("The model already has a component named {0}. Try something else!".format(name))
            return
        c = Telluric(name, self.data, rvs_fixed=rvs_fixed, variable_bases=variable_bases)
        self.components.append(c)
        self.component_names.append(name)
                                
class Component(object):
    """
    Generic class for an additive component in the spectral model.
    """
    def __init__(self, name, data, rvs_fixed=False, variable_bases=0, regularization_file='regularization/default.pkl'):
        self.data = data
        self.name = name
        self.K = variable_bases # number of variable basis vectors
        self.rvs_block = [tf.Variable(np.zeros(data.N), dtype=T, name='rvs_order{0}'.format(r)) for r in range(data.R)]
        self.ivars_block = [tf.constant(np.zeros(data.N) + 10., dtype=T, name='ivars_order{0}'.format(r)) for r in range(data.R)] # TODO
        self.rvs_fixed = rvs_fixed
        self.time_rvs = np.zeros(data.N) # will be replaced in combine_orders()
        self.order_rvs = np.zeros(data.R) # will be replaced in combine_orders()
        self.order_sigmas = np.ones(data.R) # will be replaced in combine_orders()
        self.template_xs = [tf.constant(0., dtype=T) for r in range(data.R)] # this will be replaced
        self.template_ys = [tf.constant(0., dtype=T) for r in range(data.R)] # this will be replaced
        self.basis_vectors = [tf.constant(0., dtype=T) for r in range(data.R)] # this will be replaced
        self.basis_weights = [tf.constant(0., dtype=T) for r in range(data.R)] # this will be replaced
        self.template_exists = [False for r in range(data.R)] # if True, skip initialization
        self.learning_rate_rvs = 10. # default
        self.learning_rate_template = 0.01 # default
        self.learning_rate_basis = 0.01 # default
        try: # load pickle
            reg_amps = pickle.load(open(regularization_file, 'rb'))
            self.L1_template = [reg_amps.L1_template[r] for r in data.orders]
            self.L2_template = [reg_amps.L2_template[r] for r in data.orders]
            self.L1_basis_vectors = [reg_amps.L1_basis_vectors[r] for r in data.orders]
            self.L2_basis_vectors = [reg_amps.L2_basis_vectors[r] for r in data.orders]
            self.L2_basis_weights = [reg_amps.L2_basis_weights[r] for r in data.orders]                 
        except: # file doesn't work - take defaults
            if regularization_file == 'regularization/default.pkl':
                print('no regularization amplitudes specified - taking defaults')
            else:
                print('regularization amplitudes in file {0} are not of the correct form - taking defaults'.format(regularization_file))
            self.L1_template = [0. for r in range(data.R)]
            self.L2_template = [0. for r in range(data.R)]
            self.L1_basis_vectors = [0. for r in range(data.R)]
            self.L2_basis_vectors = [0. for r in range(data.R)]
            self.L2_basis_weights = [1. for r in range(data.R)]
        
    def shift_and_interp(self, r, rvs):
        """
        Apply Doppler shift of rvs to the model at order r and output interpolated values at data xs.
        """
        shifted_xs = self.data.xs[r] + tf.log(doppler(rvs[:, None]))
        return interp(shifted_xs, self.template_xs[r], self.template_ys[r]) 
        
    def synthesize(self, r):
        """
        Output synthesized spectrum for order r.
        """
        if self.template_exists[r]:
            synth = self.shift_and_interp(r, self.rvs_block[r])
            if self.K > 0:
                synth += tf.matmul(self.basis_weights[r], self.basis_vectors[r])
        else:
            synth = tf.zeros_like(self.data.xs[r])
        return synth
        
    def initialize_template(self, r, data, other_components=None, template_xs=None):
        """
        Doppler-shift data into component rest frame, subtract off other components, 
        and average to make a composite spectrum.
        """
        shifted_xs = data.xs[r] + tf.log(doppler(self.rvs_block[r][:, None])) # component rest frame
        if template_xs is None:
            dx = tf.constant(2.*(np.log(6000.01) - np.log(6000.)), dtype=T) # log-uniform spacing
            tiny = tf.constant(10., dtype=T)
            template_xs = tf.range(tf.reduce_min(shifted_xs)-tiny*dx, 
                                   tf.reduce_max(shifted_xs)+tiny*dx, dx)           
        resids = 1. * data.ys[r]
        for c in other_components: # subtract off initialized components
            if c.template_exists[r]:
                resids -= c.shift_and_interp(r, c.rvs_block[r])
                
        session = get_session()
        session.run(tf.global_variables_initializer())
        template_xs, template_ys = bin_data(session.run(shifted_xs), session.run(resids), 
                                            session.run(template_xs)) # hack
        self.template_xs[r] = tf.Variable(template_xs, dtype=T, name='template_xs')
        self.template_ys[r] = tf.Variable(template_ys, dtype=T, name='template_ys') 
        if self.K > 0:
            # initialize basis components
            resids -= self.shift_and_interp(r, self.rvs_block[r])
            s,u,v = tf.svd(resids, compute_uv=True)
            basis_vectors = tf.transpose(tf.conj(v[:,:self.K])) # eigenspectra (K x M)
            basis_weights = (u * s)[:,:self.K] # weights (N x K)
            self.basis_vectors[r] = tf.Variable(basis_vectors, dtype=T, name='basis_vectors')
            self.basis_weights[r] = tf.Variable(basis_weights, dtype=T, name='basis_weights') 
            session.run(tf.variables_initializer([self.basis_vectors[r], self.basis_weights[r]]))  # TODO: more elegant way to do this?
        session.run(tf.variables_initializer([self.template_xs[r], self.template_ys[r]]))  # TODO: more elegant way to do this?
        self.template_exists[r] = True
         
        
    def make_optimizers(self, r, nll, learning_rate_rvs=None, 
            learning_rate_template=None, learning_rate_basis=None):
        # TODO: make each one an R-length list rather than overwriting each order?
        if learning_rate_rvs == None:
            learning_rate_rvs = self.learning_rate_rvs
        if learning_rate_template == None:
            learning_rate_template = self.learning_rate_template
        if learning_rate_basis == None:
            learning_rate_basis = self.learning_rate_basis
        self.gradients_template = tf.gradients(nll, self.template_ys[r])
        self.opt_template = tf.train.AdamOptimizer(learning_rate_template).minimize(nll, 
                            var_list=[self.template_ys[r]])
        if not self.rvs_fixed:
            self.gradients_rvs = tf.gradients(nll, self.rvs_block[r])
            self.opt_rvs = tf.train.AdamOptimizer(learning_rate_rvs).minimize(nll, 
                            var_list=[self.rvs_block[r]])
        if self.K > 0:
            self.gradients_basis = tf.gradients(nll, [self.basis_vectors[r], self.basis_weights[r]])
            self.opt_basis = tf.train.AdamOptimizer(learning_rate_basis).minimize(nll, 
                            var_list=[self.basis_vectors[r], self.basis_weights[r]]) 
                              
    def combine_orders(self):
        self.all_rvs = np.asarray(session.run(self.rvs_block))
        self.all_ivars = np.asarray(session.run(self.ivars_block))
        # initial guess
        x0_order_rvs = np.median(self.all_rvs, axis=1)
        x0_time_rvs = np.median(self.all_rvs - np.tile(x0_order_rvs[:,None], (1, self.N)), axis=0)
        rv_predictions = np.tile(x0_order_rvs[:,None], (1,self.data.N)) + np.tile(x0_time_rvs, (self.data.R,1))
        x0_sigmas = np.log(np.var(self.all_rvs - rv_predictions, axis=1))
        self.M = None
        # optimize
        soln_sigmas = minimize(self.opposite_lnlike_sigmas, x0_sigmas, args=(restart), method='BFGS', options={'disp':True})['x'] # HACK
        # save results
        lnlike, rvs_N, rvs_R = self.lnlike_sigmas(soln_sigmas, return_rvs=True)
        self.time_rvs = rvs_N
        self.order_rvs = rvs_R
        self.order_sigmas = soln_sigmas 
        
    def pack_rv_pars(self, time_rvs, order_rvs, order_sigmas):
        rv_pars = np.append(time_rvs, order_rvs)
        rv_pars = np.append(rv_pars, order_sigmas)
        return rv_pars
    
    def unpack_rv_pars(self, rv_pars):
        self.time_rvs = np.copy(rv_pars[:self.N])
        self.order_rvs = np.copy(rv_pars[self.N:self.R + self.N])
        self.order_sigmas = np.copy(rv_pars[self.R + self.N:])
        return self.time_rvs, self.order_rvs, self.order_sigmas
        
    def lnlike_sigmas(self, sigmas, return_rvs = False, restart = False):
        assert len(sigmas) == self.data.R
        M = self.get_design_matrix(restart = restart)
        something = np.zeros_like(M[0,:])
        something[self.data.N:] = 1. / self.data.R # last datum will be mean of order velocities is zero
        M = np.append(M, something[None, :], axis=0) # last datum
        Rs, Ns = self.get_index_lists()
        ivars = 1. / ((1. / self.all_ivars) + sigmas[Rs]**2) # not zero-safe
        ivars = ivars.flatten()
        ivars = np.append(ivars, 1.) # last datum: MAGIC
        MTM = np.dot(M.T, ivars[:, None] * M)
        ys = self.all_rvs.flatten()
        ys = np.append(ys, 0.) # last datum
        MTy = np.dot(M.T, ivars * ys)
        xs = np.linalg.solve(MTM, MTy)
        resids = ys - np.dot(M, xs)
        lnlike = -0.5 * np.sum(resids * ivars * resids - np.log(2. * np.pi * ivars))
        if return_rvs:
            return lnlike, xs[:self.data.N], xs[self.data.N:] # must be synchronized with get_design_matrix(), and last datum removal
        return lnlike
        
    def opposite_lnlike_sigmas(self, pars, **kwargs):
        return -1. * self.lnlike_sigmas(pars, **kwargs)    

    def get_index_lists(self):
        return np.mgrid[:self.data.R, :self.data.N]

    def get_design_matrix(self, restart = False):
        if (self.M is None) or restart:
            Rs, Ns = self.get_index_lists()
            ndata = self.data.R * self.data.N
            self.M = np.zeros((ndata, self.data.N + self.data.R)) # note design choices
            self.M[range(ndata), Ns.flatten()] = 1.
            self.M[range(ndata), self.data.N + Rs.flatten()] = 1.
            return self.M
        else:
            return self.M      

class Star(Component):
    """
    A star (or generic celestial object)
    """
    def __init__(self, name, data, rvs_fixed=False, variable_bases=0, regularization_file='regularization/default.pkl'):
        Component.__init__(self, name, data, rvs_fixed=rvs_fixed, variable_bases=variable_bases, regularization_file=regularization_file)
        starting_rvs = np.copy(data.bervs) - np.mean(data.bervs)
        self.rvs_block = [tf.Variable(starting_rvs, dtype=T, name='rvs_order{0}'.format(r)) for r in range(data.R)] 
                            
class Telluric(Component):
    """
    Sky absorption
    """
    def __init__(self, name, data, rvs_fixed=True, variable_bases=0, regularization_file='regularization/default.pkl'):
        Component.__init__(self, name, data, rvs_fixed=rvs_fixed, variable_bases=variable_bases, regularization_file=regularization_file)
        self.airms = tf.constant(data.airms, dtype=T)
        self.learning_rate_template = 0.1
        
    def synthesize(self, r):
        synth = Component.synthesize(self, r)
        return tf.einsum('n,nm->nm', self.airms, synth)
        
class History(object):
    """
    Information about optimization history of a single order stored in numpy arrays/lists
    """   
    def __init__(self, model, data, r, niter, filename=None):
        for c in model.components:
            assert c.template_exists[r], "ERROR: Cannot initialize History() until templates are initialized."
        self.nll_history = np.empty(niter)
        self.rvs_history = [np.empty((niter, data.N)) for c in model.components]
        self.template_history = [np.empty((niter, int(c.template_ys[r].shape[0]))) for c in model.components]
        self.basis_vectors_history = [np.empty((niter, c.K, 4096)) for c in model.components] # HACK
        self.basis_weights_history = [np.empty((niter, data.N, c.K)) for c in model.components]
        self.chis_history = np.empty((niter, data.N, 4096)) # HACK
        self.r = r
        self.niter = niter
        if filename is not None:
            self.read(filename)
        
    def save_iter(self, model, data, i, nll, chis):
        """
        Save all necessary information at optimization step i
        """
        session = get_session()
        self.nll_history[i] = session.run(nll)
        self.chis_history[i,:,:] = np.copy(session.run(chis))
        for j,c in enumerate(model.components):
            template_state = session.run(c.template_ys[self.r])
            rvs_state = session.run(c.rvs_block[self.r])
            self.template_history[j][i,:] = np.copy(template_state)
            self.rvs_history[j][i,:] = np.copy(rvs_state)  
            if c.K > 0:
                self.basis_vectors_history[j][i,:,:] = np.copy(session.run(c.basis_vectors[self.r])) 
                self.basis_weights_history[j][i,:,:] = np.copy(session.run(c.basis_weights[self.r]))
        
    def write(self, filename=None):
        """
        Write to hdf5
        """
        if filename is None:
            filename = 'order{0}_history.hdf5'.format(self.r)
        print("saving optimization history to {0}".format(filename))
        with h5py.File(filename,'w') as f:
            for attr in ['nll_history', 'chis_history', 'r', 'niter']:
                f.create_dataset(attr, data=getattr(self, attr))
            for attr in ['rvs_history', 'template_history', 'basis_vectors_history', 'basis_weights_history']:
                for i in range(len(self.template_history)):
                    f.create_dataset(attr+'_{0}'.format(i), data=getattr(self, attr)[i])   
                    
    
    def read(self, filename):
        """
        Read from hdf5
        """         
        with h5py.File(filename, 'r') as f:
            for attr in ['nll_history', 'chis_history', 'r', 'niter']:
                setattr(self, attr, np.copy(f[attr]))
            for attr in ['rvs_history', 'template_history', 'basis_vectors_history', 'basis_weights_history']:
                d = []
                for i in range(len(self.template_history)):
                    d.append(np.copy(f[attr+'_{0}'.format(i)]))
                setattr(self, attr, d)
                
        
    def animfunc(self, i, xs, ys, xlims, ylims, ax, driver):
        """
        Produces each frame; called by History.plot()
        """
        ax.cla()
        ax.set_xlim(xlims)
        ax.set_ylim(ylims)
        ax.set_title('Optimization step #{0}'.format(i))
        s = driver(xs, ys[i,:])
        
    def plot(self, xs, ys, linestyle, nframes=None, ylims=None):
        """
        Generate a matplotlib animation of xs and ys
        Linestyle options: 'scatter', 'line'
        """
        if nframes is None:
            nframes = self.niter
        fig = plt.figure()
        ax = plt.subplot() 
        if linestyle == 'scatter':
            driver = ax.scatter
        elif linestyle == 'line':
            driver = ax.plot
        else:
            print("linestyle not recognized.")
            return
        x_pad = (np.max(xs) - np.min(xs)) * 0.1
        xlims = (np.min(xs)-x_pad, np.max(xs)+x_pad)
        if ylims is None:
            y_pad = (np.max(ys) - np.min(ys)) * 0.1
            ylims = (np.min(ys)-y_pad, np.max(ys)+y_pad)
        ani = animation.FuncAnimation(fig, self.animfunc, np.linspace(0, self.niter-1, nframes, dtype=int), 
                    fargs=(xs, ys, xlims, ylims, ax, driver), interval=150)
        plt.close(fig)
        return ani  
                         
    def plot_rvs(self, ind, model, data, compare_to_pipeline=True, **kwargs):
        """
        Generate a matplotlib animation of RVs vs. time
        ind: index of component in model to be plotted
        compare_to_pipeline keyword subtracts off the HARPS DRS values (useful for removing BERVs)
        """
        xs = data.dates
        ys = self.rvs_history[ind]
        if compare_to_pipeline:
            ys -= np.repeat([data.pipeline_rvs], self.niter, axis=0)    
        return self.plot(xs, ys, 'scatter', **kwargs)     
    
    def plot_template(self, ind, model, data, **kwargs):
        """
        Generate a matplotlib animation of the template inferred from data
        ind: index of component in model to be plotted
        """
        session = get_session()
        template_xs = session.run(model.components[ind].template_xs[self.r])
        xs = np.exp(template_xs)
        ys = np.exp(self.template_history[ind])
        return self.plot(xs, ys, 'line', **kwargs) 
    
    def plot_chis(self, epoch, model, data, **kwargs):
        """
        Generate a matplotlib animation of model chis in data space
        epoch: index of epoch to plot
        """
        session = get_session()
        data_xs = session.run(data.xs[self.r][epoch,:])
        xs = np.exp(data_xs)
        ys = self.chis_history[:,epoch,:]
        return self.plot(xs, ys, 'line', **kwargs)   
        
class Results(object):
    def __init__(self, model=None, data=None, filename=None):
        if data is not None and model is not None:
            self.copy_data(data)
            self.copy_model(model)
        elif filename is not None:
            self.read(filename)
        else:
            print("ERROR: Results() object must have model and data keywords OR filename keyword to initialize.")            
            
    def copy_data(self, data):
        for attr in DATA_NP_ATTRS:
            setattr(self, attr, getattr(data,attr))   
        session = get_session()
        for attr in DATA_TF_ATTRS:
            setattr(self, attr, session.run(getattr(data,attr)))
            
    def copy_model(self, model):
        self.component_names = model.component_names
        session = get_session()
        self.ys_predicted = [session.run(model.synthesize(r)) for r in range(self.R)]
        for c in model.components:
            basename = c.name+'_'
            ys_predicted = [session.run(c.synthesize(r)) for r in range(self.R)]
            setattr(self, basename+'ys_predicted', ys_predicted)
            for attr in COMPONENT_NP_ATTRS:
                setattr(self, basename+attr, getattr(c,attr))
            for attr in COMPONENT_TF_ATTRS:
                try:
                    setattr(self, basename+attr, session.run(getattr(c,attr)))
                except: # catch when basis vectors are Nones
                    assert c.K == 0, "Results: copy_model() failed on attribute {0}".format(attr)
                    
    def update_order_model(self, model, r):
        session = get_session()
        self.ys_predicted[r] = session.run(model.synthesize(r))
        for c in model.components:
            basename = c.name+'_'
            ys_predicted = session.run(c.synthesize(r))
            getattr(self, basename+'ys_predicted')[r] = ys_predicted
            for attr in COMPONENT_NP_ATTRS:
                if type(getattr(c,attr)) == list: # skip attributes common to all orders
                    getattr(self, basename+attr)[r] = getattr(c,attr)[r]
            for attr in COMPONENT_TF_ATTRS:
                try:
                    getattr(self, basename+attr)[r] = session.run(getattr(c,attr)[r])
                except: # catch when basis vectors are Nones
                    assert c.K == 0, "Results: update_order_model() failed on attribute {0}".format(attr)
                    
    def compute_final_rvs(self):
        for c in model.components:
            if not c.rvs_fixed:
                c.combine_orders()
                basename = c.name+'_'
                setattr(self, basename+'time_rvs', c.time_rvs)
                setattr(self, basename+'order_rvs', c.order_rvs)
                setattr(self, basename+'order_sigmas', c.order_sigmas)
                        
    def read(self, filename):
        print("Results: reading from {0}".format(filename))
        with h5py.File(filename,'r') as f:
            for attr in np.append(DATA_NP_ATTRS, DATA_TF_ATTRS):
                setattr(self, attr, np.copy(f[attr]))
            self.component_names = np.copy(f['component_names'])
            self.component_names = [a.decode('utf8') for a in self.component_names] # h5py workaround
            self.ys_predicted = np.copy(f['ys_predicted'])
            for name in self.component_names:
                basename = name + '_'
                for attr in np.append(COMPONENT_NP_ATTRS, COMPONENT_TF_ATTRS):
                    try:
                        setattr(self, basename+attr, np.copy(f[basename+attr]))
                    except: # catch when basis vectors are Nones
                        assert np.copy(f[basename+'K']) == 0, "Results: read() failed on attribute {0}".format(basename+attr)
                setattr(self, basename+'ys_predicted', np.copy(f[basename+'ys_predicted']))
                    
    def write(self, filename):
        print("Results: writing to {0}".format(filename))
        self.component_names = [a.encode('utf8') for a in self.component_names] # h5py workaround
        with h5py.File(filename,'w') as f:
            for attr in vars(self):
                f.create_dataset(attr, data=getattr(self, attr))         
            

def optimize_order(model, data, r, results=None, niter=100, save_every=100, save_history=False, basename='wobble'):
    '''
    optimize the model for order r in data
    '''      
    for c in model.components:
        if ~c.template_exists[r]:
            c.initialize_template(r, data, other_components=[x for x in model.components if x!=c])
                
    # likelihood calculation:
    synth = model.synthesize(r)
    chis = (data.ys[r] - synth) * tf.sqrt(data.ivars[r])
    nll = 0.5*tf.reduce_sum(tf.square(tf.boolean_mask(data.ys[r], data.epoch_mask) 
                                      - tf.boolean_mask(synth, data.epoch_mask)) 
                            * tf.boolean_mask(data.ivars[r], data.epoch_mask))
    
    # regularization:
    for c in model.components:
        nll += c.L1_template[r] * tf.reduce_sum(tf.abs(c.template_ys[r]))
        nll += c.L2_template[r] * tf.reduce_sum(tf.square(c.template_ys[r]))
        if c.K > 0:
            nll += c.L1_basis_vectors[r] * tf.reduce_sum(tf.abs(c.basis_vectors[r]))
            nll += c.L2_basis_vectors[r] * tf.reduce_sum(tf.square(c.basis_vectors[r]))
            nll += c.L2_basis_weights[r] * tf.reduce_sum(tf.square(c.basis_weights[r]))
        
    # set up optimizers: 
    for c in model.components:
        c.make_optimizers(r, nll)

    session = get_session()
    session.run(tf.global_variables_initializer())  # TODO: is this overwriting anything important?
    
    # initialize helper classes:
    if save_history:
        history = History(model, data, r, niter)   
    if results is None: 
        results = Results(model=model, data=data)
        
    # optimize:
    for i in tqdm(range(niter), total=niter, miniters=int(niter/10)):
        if save_history:
            history.save_iter(model, data, i, nll, chis)           
        for c in model.components:
            if not c.rvs_fixed:            
                session.run(c.opt_rvs) # optimize RVs
            session.run(c.opt_template) # optimize mean template
            if c.K > 0:
                session.run(c.opt_basis) # optimize variable components
        if (i+1 % save_every == 0): # progress save
            results.copy_model(model) # update
            results.write(basename+'_results.hdf5'.format(r))
            if save_history:
                history.write(basename+'_o{0}_history.hdf5'.format(r))
                
    if save_history: # final post-optimization save
        history.write(basename+'_o{0}_history.hdf5'.format(r))
    results.update_order_model(model, r) # update
    return results

def optimize_orders(model, data, **kwargs):
    """
    optimize model for all orders in data
    """
    session = get_session()
    #session.run(tf.global_variables_initializer())    # should this be in get_session?
    for r in range(data.R):
        print("--- ORDER {0} ---".format(r))
        if r == 0: 
            results = optimize_order(model, data, r, **kwargs)
        else:
            results = optimize_order(model, data, r, results=results, **kwargs)
        #if (r % 5) == 0:
        #    results.write('results_order{0}.hdf5'.format(r))
    results.write('results.hdf5')    
    return results    