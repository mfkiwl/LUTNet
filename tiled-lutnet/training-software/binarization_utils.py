import numpy as np
import pickle
import matplotlib.pyplot as plt
import matplotlib

import tensorflow as tf
import keras
from keras.models import Sequential, Model
from keras.layers import Dense, Convolution2D, Activation, Flatten, MaxPooling2D,Input,Dropout,GlobalAveragePooling2D
from keras import backend as K
from keras.datasets import cifar10
from keras.utils import np_utils
from keras.optimizers import SGD
from keras.engine.topology import Layer
from keras.models import load_model
from keras.preprocessing.image import ImageDataGenerator
import os
from keras.layers.normalization import BatchNormalization
from tensorflow.python.framework import ops
#from multi_gpu import make_parallel

def binarize(x):
    '''Element-wise rounding to the closest integer with full gradient propagation.
    A trick from [Sergey Ioffe](http://stackoverflow.com/a/36480182)
    '''
    clipped = K.clip(x,-1,1)
    rounded = K.sign(clipped)
    return clipped + K.stop_gradient(rounded - clipped)

class Residual_sign(Layer):
    def __init__(self, levels=1,trainable=True,**kwargs):
        self.levels=levels
        self.trainable=trainable
        super(Residual_sign, self).__init__(**kwargs)
    def build(self, input_shape):
        ars=np.arange(self.levels)+1.0
        ars=ars[::-1]
        means=ars/np.sum(ars)
        self.means = self.add_weight(name='means',
            shape=(self.levels, ),
            initializer=keras.initializers.Constant(value=means),
            trainable=self.trainable) # Trainable scaling factors for residual binarisation
    def call(self, x, mask=None):
        resid = x
        out_bin=0

        if self.levels==1:
            for l in range(self.levels):
                out=binarize(resid)*abs(self.means[l])
                out_bin=out_bin+out
                resid=resid-out
        elif self.levels==2:
            out=binarize(resid)*abs(self.means[0])
            out_bin=out
            resid=resid-out
            out=binarize(resid)*abs(self.means[1])
            out_bin=tf.stack([out_bin,out]) # Add one extra dimension to activations: binary levels
            resid=resid-out
                
        return out_bin

    def get_output_shape_for(self,input_shape):
        if self.levels==1:
            return input_shape
        else:
            return (self.levels, input_shape)
    def compute_output_shape(self,input_shape):
        if self.levels==1:
            return input_shape
        else:
            return (self.levels, input_shape)
    def set_means(self,X):
        means=np.zeros((self.levels))
        means[0]=1
        resid=np.clip(X,-1,1)
        approx=0
        for l in range(self.levels):
            m=np.mean(np.absolute(resid))
            out=np.sign(resid)*m
            approx=approx+out
            resid=resid-out
            means[l]=m
            err=np.mean((approx-np.clip(X,-1,1))**2)

        means=means/np.sum(means)
        sess=K.get_session()
        sess.run(self.means.assign(means))

class binary_conv(Layer):
	def __init__(self,nfilters,ch_in,k,padding,strides=(1,1),levels=1,pruning_prob=0,first_layer=False,LUT=True,BINARY=True,TM=1,TN=1,**kwargs):
		self.nfilters=nfilters
		self.ch_in=ch_in
		self.k=k
		self.padding=padding
		if padding=='valid':
			self.PADDING = "VALID" # tf uses upper-case padding notations whereas keras uses lower-case notations
		elif padding=='same':
			self.PADDING = "SAME"
		self.strides=strides
		self.levels=levels # number of binary levels
		self.first_layer=first_layer # bool flag for being the 1st layer (in BNN, input activations of the 1st layer are always in fxp)
		self.LUT=LUT # bool flag for whether to train with LUTNet architecture
		self.BINARY=BINARY # bool flag for, if LUT==True, whether to train with binary weights
		self.window_size=self.ch_in*self.k*self.k # size of the input activation sliding window
		self.TM = TM # tiling factor wrt input channels
		self.TN = TN # tiling factor wrt output cnannels
		self.tile_size=[self.k,self.k,self.ch_in/self.TM,self.nfilters/self.TN]
		super(binary_conv,self).__init__(**kwargs)
	def build(self, input_shape):

		self.rand_map_0 = self.add_weight(name='rand_map_0', 
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], size=[self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1])),
			trainable=False) # Randomisation map for 2nd input connections
		self.rand_map_1 = self.add_weight(name='rand_map_1', 
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], size=[self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1])),
			trainable=False) # Randomisation map for 3rd input connections
		self.rand_map_2 = self.add_weight(name='rand_map_2', 
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0]*self.tile_size[1]*self.tile_size[2], size=[self.tile_size[0]*self.tile_size[1]*self.tile_size[2], 1])),
			trainable=False) # Randomisation map for 4th input connections

		self.rand_map_exp_0 = self.add_weight(name='rand_map_exp_0', 
			shape=(self.window_size, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.window_size, size=[self.window_size, 1])),
			trainable=False) # 1st randomisation map unrolled
		self.rand_map_exp_1 = self.add_weight(name='rand_map_exp_1', 
			shape=(self.window_size, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.window_size, size=[self.window_size, 1])),
			trainable=False) # 2nd randomisation map unrolled
		self.rand_map_exp_2 = self.add_weight(name='rand_map_exp_2', 
			shape=(self.window_size, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.window_size, size=[self.window_size, 1])),
			trainable=False) # 3rd randomisation map unrolled

		stdv=1/np.sqrt(self.k*self.k*self.ch_in)
		self.gamma=K.variable(1.0)

		if self.levels==1 or self.first_layer==True:
			w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
			self.w=K.variable(w)
			self.trainable_weights=[self.w,self.gamma]
		elif self.levels==2:
			if self.LUT==True:

				# w: BRAM contents
				# c: LUT binary masks
				w1  = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				c1  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c2  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c3  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c4  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c5  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c6  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c7  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c8  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c9  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c10 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c11 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c12 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c13 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c14 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c15 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c16 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c17 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c18 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c19 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c20 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c21 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c22 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c23 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c24 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c25 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c26 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c27 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c28 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c29 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c30 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c31 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c32 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				
				self.c1 =K.variable(c1)
				self.c2 =K.variable(c2)
				self.c3 =K.variable(c3)
				self.c4 =K.variable(c4)
				self.c5 =K.variable(c5)
				self.c6 =K.variable(c6)
				self.c7 =K.variable(c7)
				self.c8 =K.variable(c8)
				self.c9 =K.variable(c9)
				self.c10=K.variable(c10)
				self.c11=K.variable(c11)
				self.c12=K.variable(c12)
				self.c13=K.variable(c13)
				self.c14=K.variable(c14)
				self.c15=K.variable(c15)
				self.c16=K.variable(c16)
				self.c17=K.variable(c17)
				self.c18=K.variable(c18)
				self.c19=K.variable(c19)
				self.c20=K.variable(c20)
				self.c21=K.variable(c21)
				self.c22=K.variable(c22)
				self.c23=K.variable(c23)
				self.c24=K.variable(c24)
				self.c25=K.variable(c25)
				self.c26=K.variable(c26)
				self.c27=K.variable(c27)
				self.c28=K.variable(c28)
				self.c29=K.variable(c29)
				self.c30=K.variable(c30)
				self.c31=K.variable(c31)
				self.c32=K.variable(c32)
				self.w1 =K.variable(w1)

				self.trainable_weights=[self.c1,self.c2,self.c3,self.c4,self.c5,self.c6,self.c7,self.c8,self.c9,self.c10,self.c11,self.c12,self.c13,self.c14,self.c15,self.c16,
					self.c17,self.c18,self.c19,self.c20,self.c21,self.c22,self.c23,self.c24,self.c25,self.c26,self.c27,self.c28,self.c29,self.c30,self.c31,self.c32,
					self.w1,self.gamma]

			else:
				w = np.random.normal(loc=0.0, scale=stdv,size=[self.k,self.k,self.ch_in,self.nfilters]).astype(np.float32)
				self.w=K.variable(w)
				self.trainable_weights=[self.w,self.gamma]
	
		self.pruning_mask = self.add_weight(name='pruning_mask',
			shape=(self.tile_size[0]*self.tile_size[1]*self.tile_size[2],self.tile_size[3]),
			initializer=keras.initializers.Constant(value=np.ones((self.tile_size[0]*self.tile_size[1]*self.tile_size[2],self.tile_size[3]))),
			trainable=False) # LUT pruning mask




	def call(self, x,mask=None):
		constraint_gamma=K.abs(self.gamma) # Gamma is the current layer's trainable scaling factor. One per layer.

		if self.levels==1 or self.first_layer==True: # 1st layer cannot use LUTNet architecture because inputs are always in fxp
			if self.BINARY==False:
				self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
			else:
				self.clamped_w=constraint_gamma*binarize(self.w)
		elif self.levels==2:
			if self.LUT==True:
				if self.BINARY==False:
					self.clamped_w1 =K.clip(self.w1,-1,1) # w is not affacted by tiling

					self.clamped_c1 =constraint_gamma*K.clip(tf.tile(self.c1,  [1,1,self.TM,self.TN]),-1,1) # when trained, c is tiled and therefore shared by multiple tiles of input activations
					self.clamped_c2 =constraint_gamma*K.clip(tf.tile(self.c2,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c3 =constraint_gamma*K.clip(tf.tile(self.c3,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c4 =constraint_gamma*K.clip(tf.tile(self.c4,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c5 =constraint_gamma*K.clip(tf.tile(self.c5,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c6 =constraint_gamma*K.clip(tf.tile(self.c6,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c7 =constraint_gamma*K.clip(tf.tile(self.c7,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c8 =constraint_gamma*K.clip(tf.tile(self.c8,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c9 =constraint_gamma*K.clip(tf.tile(self.c9,  [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c10=constraint_gamma*K.clip(tf.tile(self.c10, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c11=constraint_gamma*K.clip(tf.tile(self.c11, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c12=constraint_gamma*K.clip(tf.tile(self.c12, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c13=constraint_gamma*K.clip(tf.tile(self.c13, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c14=constraint_gamma*K.clip(tf.tile(self.c14, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c15=constraint_gamma*K.clip(tf.tile(self.c15, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c16=constraint_gamma*K.clip(tf.tile(self.c16, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c17=constraint_gamma*K.clip(tf.tile(self.c17, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c18=constraint_gamma*K.clip(tf.tile(self.c18, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c19=constraint_gamma*K.clip(tf.tile(self.c19, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c20=constraint_gamma*K.clip(tf.tile(self.c20, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c21=constraint_gamma*K.clip(tf.tile(self.c21, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c22=constraint_gamma*K.clip(tf.tile(self.c22, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c23=constraint_gamma*K.clip(tf.tile(self.c23, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c24=constraint_gamma*K.clip(tf.tile(self.c24, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c25=constraint_gamma*K.clip(tf.tile(self.c25, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c26=constraint_gamma*K.clip(tf.tile(self.c26, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c27=constraint_gamma*K.clip(tf.tile(self.c27, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c28=constraint_gamma*K.clip(tf.tile(self.c28, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c29=constraint_gamma*K.clip(tf.tile(self.c29, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c30=constraint_gamma*K.clip(tf.tile(self.c30, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c31=constraint_gamma*K.clip(tf.tile(self.c31, [1,1,self.TM,self.TN]),-1,1)
					self.clamped_c32=constraint_gamma*K.clip(tf.tile(self.c32, [1,1,self.TM,self.TN]),-1,1)

				else:

					self.clamped_w1 =binarize(self.w1)

					self.clamped_c1 =constraint_gamma*binarize(tf.tile(self.c1, [1,1,self.TM,self.TN]))
					self.clamped_c2 =constraint_gamma*binarize(tf.tile(self.c2, [1,1,self.TM,self.TN]))
					self.clamped_c3 =constraint_gamma*binarize(tf.tile(self.c3, [1,1,self.TM,self.TN]))
					self.clamped_c4 =constraint_gamma*binarize(tf.tile(self.c4, [1,1,self.TM,self.TN]))
					self.clamped_c5 =constraint_gamma*binarize(tf.tile(self.c5, [1,1,self.TM,self.TN]))
					self.clamped_c6 =constraint_gamma*binarize(tf.tile(self.c6, [1,1,self.TM,self.TN]))
					self.clamped_c7 =constraint_gamma*binarize(tf.tile(self.c7, [1,1,self.TM,self.TN]))
					self.clamped_c8 =constraint_gamma*binarize(tf.tile(self.c8, [1,1,self.TM,self.TN]))
					self.clamped_c9 =constraint_gamma*binarize(tf.tile(self.c9, [1,1,self.TM,self.TN]))
					self.clamped_c10=constraint_gamma*binarize(tf.tile(self.c10,[1,1,self.TM,self.TN]))
					self.clamped_c11=constraint_gamma*binarize(tf.tile(self.c11,[1,1,self.TM,self.TN]))
					self.clamped_c12=constraint_gamma*binarize(tf.tile(self.c12,[1,1,self.TM,self.TN]))
					self.clamped_c13=constraint_gamma*binarize(tf.tile(self.c13,[1,1,self.TM,self.TN]))
					self.clamped_c14=constraint_gamma*binarize(tf.tile(self.c14,[1,1,self.TM,self.TN]))
					self.clamped_c15=constraint_gamma*binarize(tf.tile(self.c15,[1,1,self.TM,self.TN]))
					self.clamped_c16=constraint_gamma*binarize(tf.tile(self.c16,[1,1,self.TM,self.TN]))
					self.clamped_c17=constraint_gamma*binarize(tf.tile(self.c17,[1,1,self.TM,self.TN]))
					self.clamped_c18=constraint_gamma*binarize(tf.tile(self.c18,[1,1,self.TM,self.TN]))
					self.clamped_c19=constraint_gamma*binarize(tf.tile(self.c19,[1,1,self.TM,self.TN]))
					self.clamped_c20=constraint_gamma*binarize(tf.tile(self.c20,[1,1,self.TM,self.TN]))
					self.clamped_c21=constraint_gamma*binarize(tf.tile(self.c21,[1,1,self.TM,self.TN]))
					self.clamped_c22=constraint_gamma*binarize(tf.tile(self.c22,[1,1,self.TM,self.TN]))
					self.clamped_c23=constraint_gamma*binarize(tf.tile(self.c23,[1,1,self.TM,self.TN]))
					self.clamped_c24=constraint_gamma*binarize(tf.tile(self.c24,[1,1,self.TM,self.TN]))
					self.clamped_c25=constraint_gamma*binarize(tf.tile(self.c25,[1,1,self.TM,self.TN]))
					self.clamped_c26=constraint_gamma*binarize(tf.tile(self.c26,[1,1,self.TM,self.TN]))
					self.clamped_c27=constraint_gamma*binarize(tf.tile(self.c27,[1,1,self.TM,self.TN]))
					self.clamped_c28=constraint_gamma*binarize(tf.tile(self.c28,[1,1,self.TM,self.TN]))
					self.clamped_c29=constraint_gamma*binarize(tf.tile(self.c29,[1,1,self.TM,self.TN]))
					self.clamped_c30=constraint_gamma*binarize(tf.tile(self.c30,[1,1,self.TM,self.TN]))
					self.clamped_c31=constraint_gamma*binarize(tf.tile(self.c31,[1,1,self.TM,self.TN]))
					self.clamped_c32=constraint_gamma*binarize(tf.tile(self.c32,[1,1,self.TM,self.TN]))

			else:
				if self.BINARY==False:
					self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
				else:
					self.clamped_w=constraint_gamma*binarize(self.w)

		if keras.__version__[0]=='2':

			if self.levels==1 or self.first_layer==True:
				self.out=K.conv2d(x, kernel=self.clamped_w*tf.tile(tf.reshape(self.pruning_mask, self.tile_size), [1,1,self.TM,self.TN]), padding=self.padding,strides=self.strides )
			elif self.levels==2:
				if self.LUT==True: # LUTNet
					x0_patches = tf.extract_image_patches(x[0,:,:,:,:],
						[1, self.k, self.k, 1],
						[1, self.strides[0], self.strides[1], 1], [1, 1, 1, 1],
						padding=self.PADDING) # conv dissected into im2col + dotproduct, such that windows of input actications are unrolled and randomisation is constrained within respective windows
					x1_patches = tf.extract_image_patches(x[1,:,:,:,:],
						[1, self.k, self.k, 1],
						[1, self.strides[0], self.strides[1], 1], [1, 1, 1, 1],
						padding=self.PADDING)

					# Special hack for randomising the subsequent input connections: tensorflow does not support advanced matrix indexing
					x0_shuf_patches=tf.transpose(x0_patches, perm=[3, 0, 1, 2])
					x0_shuf_patches_0 = tf.gather_nd(x0_shuf_patches, tf.cast(self.rand_map_exp_0, tf.int32))
					x0_shuf_patches_0=tf.transpose(x0_shuf_patches_0, perm=[1, 2, 3, 0])
					x0_shuf_patches_1 = tf.gather_nd(x0_shuf_patches, tf.cast(self.rand_map_exp_1, tf.int32))
					x0_shuf_patches_1=tf.transpose(x0_shuf_patches_1, perm=[1, 2, 3, 0])
					x0_shuf_patches_2 = tf.gather_nd(x0_shuf_patches, tf.cast(self.rand_map_exp_2, tf.int32))
					x0_shuf_patches_2=tf.transpose(x0_shuf_patches_2, perm=[1, 2, 3, 0])
				
					x1_shuf_patches=tf.transpose(x1_patches, perm=[3, 0, 1, 2])
					x1_shuf_patches_0 = tf.gather_nd(x1_shuf_patches, tf.cast(self.rand_map_exp_0, tf.int32))
					x1_shuf_patches_0=tf.transpose(x1_shuf_patches_0, perm=[1, 2, 3, 0])
					x1_shuf_patches_1 = tf.gather_nd(x1_shuf_patches, tf.cast(self.rand_map_exp_1, tf.int32))
					x1_shuf_patches_1=tf.transpose(x1_shuf_patches_1, perm=[1, 2, 3, 0])
					x1_shuf_patches_2 = tf.gather_nd(x1_shuf_patches, tf.cast(self.rand_map_exp_2, tf.int32))
					x1_shuf_patches_2=tf.transpose(x1_shuf_patches_2, perm=[1, 2, 3, 0])
					
					# Lagrangian interpolating polynomial
					x0_pos=(1+binarize(x0_patches))/2*abs(x0_patches) # abs(xi_patches) is trainable scaling factor for ith binary level, which is multiplied by once only per dot product
					x0_neg=(1-binarize(x0_patches))/2*abs(x0_patches)
					x1_pos=(1+binarize(x1_patches))/2*abs(x1_patches)
					x1_neg=(1-binarize(x1_patches))/2*abs(x1_patches)
					x0s0_pos=(1+binarize(x0_shuf_patches_0))/2
					x0s0_neg=(1-binarize(x0_shuf_patches_0))/2
					x1s0_pos=(1+binarize(x1_shuf_patches_0))/2
					x1s0_neg=(1-binarize(x1_shuf_patches_0))/2
					x0s1_pos=(1+binarize(x0_shuf_patches_1))/2
					x0s1_neg=(1-binarize(x0_shuf_patches_1))/2
					x1s1_pos=(1+binarize(x1_shuf_patches_1))/2
					x1s1_neg=(1-binarize(x1_shuf_patches_1))/2
					x0s2_pos=(1+binarize(x0_shuf_patches_2))/2
					x0s2_neg=(1-binarize(x0_shuf_patches_2))/2
					x1s2_pos=(1+binarize(x1_shuf_patches_2))/2
					x1s2_neg=(1-binarize(x1_shuf_patches_2))/2
				
					ws0_pos=(1+binarize(self.clamped_w1))/2
					ws0_neg=(1-binarize(self.clamped_w1))/2

					self.out=         K.dot(x0_pos*x0s0_pos*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c1 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c2 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c3 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c4 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c5 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c6 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c7 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_pos*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c8 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c9 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c10*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c11*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c12*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c13*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c14*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c15*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_pos*x0s0_neg*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c16*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c17*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c18*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c19*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c20*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c21*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c22*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c23*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_pos*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c24*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c25*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos*x0s2_pos, tf.reshape(self.clamped_c26*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c27*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_pos*x0s2_neg, tf.reshape(self.clamped_c28*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c29*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg*x0s2_pos, tf.reshape(self.clamped_c30*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c31*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x0_neg*x0s0_neg*x0s1_neg*x0s2_neg, tf.reshape(self.clamped_c32*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c1 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c2 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c3 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c4 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c5 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c6 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c7 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_pos*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c8 *ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c9 *ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c10*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c11*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c12*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c13*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c14*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c15*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_pos*x1s0_neg*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c16*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c17*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c18*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c19*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c20*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c21*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c22*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c23*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_pos*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c24*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c25*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos*x1s2_pos, tf.reshape(self.clamped_c26*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c27*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_pos*x1s2_neg, tf.reshape(self.clamped_c28*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c29*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg*x1s2_pos, tf.reshape(self.clamped_c30*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c31*ws0_pos*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))
					self.out=self.out+K.dot(x1_neg*x1s0_neg*x1s1_neg*x1s2_neg, tf.reshape(self.clamped_c32*ws0_neg*tf.tile(tf.reshape(self.pruning_mask,self.tile_size),[1,1,self.TM,self.TN]), [-1, self.nfilters]))

				else: # normal BNN
					x_expanded=0
					for l in range(self.levels):
						x_in=x[l,:,:,:,:]
						x_expanded=x_expanded+x_in
					self.out=K.conv2d(x_expanded, kernel=self.clamped_w*tf.tile(tf.reshape(self.pruning_mask, self.tile_size),[1,1,self.TM,self.TN]), padding=self.padding,strides=self.strides )
		self.output_dim=self.out.get_shape()
		return self.out
	def  get_output_shape_for(self,input_shape):
		return (input_shape[0], self.output_dim[1],self.output_dim[2],self.output_dim[3])
	def compute_output_shape(self,input_shape):
		return (input_shape[0], self.output_dim[1],self.output_dim[2],self.output_dim[3])

class binary_dense(Layer):
	def __init__(self,n_in,n_out,levels=1,pruning_prob=0,first_layer=False,LUT=True,BINARY=True,TM=1,TN=1,**kwargs):
		self.n_in=n_in
		self.n_out=n_out
		self.levels=levels # number of binary levels
		self.LUT=LUT # bool flag for whether to train with LUTNet architecture
		self.BINARY=BINARY # bool flag for, if LUT==True, whether to train with binary weights
		self.first_layer=first_layer # bool flag for being the 1st layer (in BNN, input activations of the 1st layer are always in fxp)
		self.TM = TM # tiling factor wrt input channels
		self.TN = TN # tiling factor wrt output cnannels
		self.tile_size = [n_in/TM, n_out/TN]
		super(binary_dense,self).__init__(**kwargs)
	def build(self, input_shape):
		self.rand_map_0 = self.add_weight(name='rand_map_0', 
			shape=(self.tile_size[0], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0], size=[self.tile_size[0], 1])),
			trainable=False) # Randomisation map for 2nd input connections
		self.rand_map_1 = self.add_weight(name='rand_map_1', 
			shape=(self.tile_size[0], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0], size=[self.tile_size[0], 1])),
			trainable=False) # Randomisation map for 3rd input connections
		self.rand_map_2 = self.add_weight(name='rand_map_2', 
			shape=(self.tile_size[0], 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.tile_size[0], size=[self.tile_size[0], 1])),
			trainable=False) # Randomisation map for 4th input connections

		self.rand_map_exp_0 = self.add_weight(name='rand_map_exp_0', 
			shape=(self.n_in, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.n_in, size=[self.n_in, 1])),
			trainable=False) # 1st randomisation map unrolled
		self.rand_map_exp_1 = self.add_weight(name='rand_map_exp_1', 
			shape=(self.n_in, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.n_in, size=[self.n_in, 1])),
			trainable=False) # 2nd randomisation map unrolled
		self.rand_map_exp_2 = self.add_weight(name='rand_map_exp_2', 
			shape=(self.n_in, 1),
			initializer=keras.initializers.Constant(value=np.random.randint(self.n_in, size=[self.n_in, 1])),
			trainable=False) # 3rd randomisation map unrolled

		stdv=1/np.sqrt(self.n_in)
		self.gamma=K.variable(1.0)
		if self.levels==1 or self.first_layer==True:
			w = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
			self.w=K.variable(w)
			self.trainable_weights=[self.w,self.gamma]
		elif self.levels==2:
			if self.LUT==True:

				# w: BRAM contents
				# c: LUT binary masks
				w1  = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				c1  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c2  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c3  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c4  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c5  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c6  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c7  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c8  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c9  = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c10 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c11 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c12 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c13 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c14 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c15 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c16 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c17 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c18 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c19 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c20 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c21 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c22 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c23 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c24 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c25 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c26 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c27 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c28 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c29 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c30 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c31 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)
				c32 = np.random.normal(loc=0.0, scale=stdv,size=self.tile_size).astype(np.float32)

				self.c1 =K.variable(c1)
				self.c2 =K.variable(c2)
				self.c3 =K.variable(c3)
				self.c4 =K.variable(c4)
				self.c5 =K.variable(c5)
				self.c6 =K.variable(c6)
				self.c7 =K.variable(c7)
				self.c8 =K.variable(c8)
				self.c9 =K.variable(c9)
				self.c10=K.variable(c10)
				self.c11=K.variable(c11)
				self.c12=K.variable(c12)
				self.c13=K.variable(c13)
				self.c14=K.variable(c14)
				self.c15=K.variable(c15)
				self.c16=K.variable(c16)
				self.c17=K.variable(c17)
				self.c18=K.variable(c18)
				self.c19=K.variable(c19)
				self.c20=K.variable(c20)
				self.c21=K.variable(c21)
				self.c22=K.variable(c22)
				self.c23=K.variable(c23)
				self.c24=K.variable(c24)
				self.c25=K.variable(c25)
				self.c26=K.variable(c26)
				self.c27=K.variable(c27)
				self.c28=K.variable(c28)
				self.c29=K.variable(c29)
				self.c30=K.variable(c30)
				self.c31=K.variable(c31)
				self.c32=K.variable(c32)
				self.w1 =K.variable(w1)

				self.trainable_weights=[self.c1,self.c2,self.c3,self.c4,self.c5,self.c6,self.c7,self.c8,self.c9,self.c10,self.c11,self.c12,self.c13,self.c14,self.c15,self.c16,
					self.c17,self.c18,self.c19,self.c20,self.c21,self.c22,self.c23,self.c24,self.c25,self.c26,self.c27,self.c28,self.c29,self.c30,self.c31,self.c32,
					self.w1,self.gamma]



			else:
				w = np.random.normal(loc=0.0, scale=stdv,size=[self.n_in,self.n_out]).astype(np.float32)
				self.w=K.variable(w)
				self.trainable_weights=[self.w,self.gamma]
		self.pruning_mask = self.add_weight(name='pruning_mask',
			shape=self.tile_size,
			initializer=keras.initializers.Constant(value=np.ones(self.tile_size)),
			trainable=False) # LUT pruning mask

	def call(self, x,mask=None):
		constraint_gamma=K.abs(self.gamma) # Gamma is the current layer's trainable scaling factor. One per layer.
		if self.levels==1 or self.first_layer==True: # 1st layer cannot use LUTNet architecture because inputs are always in fxp
			if self.BINARY==False:
				self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
			else:
				self.clamped_w=constraint_gamma*binarize(self.w)
			self.out=K.dot(x,self.clamped_w)
		elif self.levels==2:
			if self.LUT==True:
				if self.BINARY==False:
					self.clamped_w1=K.clip(self.w1,-1,1) # w is not affacted by tiling
	
					self.clamped_c1= constraint_gamma*K.clip(tf.tile(self.c1, [self.TM,self.TN]),-1,1) # when trained, c is tiled and therefore shared by multiple tiles of input activations
					self.clamped_c2= constraint_gamma*K.clip(tf.tile(self.c2, [self.TM,self.TN]),-1,1)
					self.clamped_c3= constraint_gamma*K.clip(tf.tile(self.c3, [self.TM,self.TN]),-1,1)
					self.clamped_c4= constraint_gamma*K.clip(tf.tile(self.c4, [self.TM,self.TN]),-1,1)
					self.clamped_c5= constraint_gamma*K.clip(tf.tile(self.c5, [self.TM,self.TN]),-1,1)
					self.clamped_c6= constraint_gamma*K.clip(tf.tile(self.c6, [self.TM,self.TN]),-1,1)
					self.clamped_c7= constraint_gamma*K.clip(tf.tile(self.c7, [self.TM,self.TN]),-1,1)
					self.clamped_c8= constraint_gamma*K.clip(tf.tile(self.c8, [self.TM,self.TN]),-1,1)
					self.clamped_c9= constraint_gamma*K.clip(tf.tile(self.c9, [self.TM,self.TN]),-1,1)
					self.clamped_c10=constraint_gamma*K.clip(tf.tile(self.c10,[self.TM,self.TN]),-1,1)
					self.clamped_c11=constraint_gamma*K.clip(tf.tile(self.c11,[self.TM,self.TN]),-1,1)
					self.clamped_c12=constraint_gamma*K.clip(tf.tile(self.c12,[self.TM,self.TN]),-1,1)
					self.clamped_c13=constraint_gamma*K.clip(tf.tile(self.c13,[self.TM,self.TN]),-1,1)
					self.clamped_c14=constraint_gamma*K.clip(tf.tile(self.c14,[self.TM,self.TN]),-1,1)
					self.clamped_c15=constraint_gamma*K.clip(tf.tile(self.c15,[self.TM,self.TN]),-1,1)
					self.clamped_c16=constraint_gamma*K.clip(tf.tile(self.c16,[self.TM,self.TN]),-1,1)
					self.clamped_c17=constraint_gamma*K.clip(tf.tile(self.c17,[self.TM,self.TN]),-1,1)
					self.clamped_c18=constraint_gamma*K.clip(tf.tile(self.c18,[self.TM,self.TN]),-1,1)
					self.clamped_c19=constraint_gamma*K.clip(tf.tile(self.c19,[self.TM,self.TN]),-1,1)
					self.clamped_c20=constraint_gamma*K.clip(tf.tile(self.c20,[self.TM,self.TN]),-1,1)
					self.clamped_c21=constraint_gamma*K.clip(tf.tile(self.c21,[self.TM,self.TN]),-1,1)
					self.clamped_c22=constraint_gamma*K.clip(tf.tile(self.c22,[self.TM,self.TN]),-1,1)
					self.clamped_c23=constraint_gamma*K.clip(tf.tile(self.c23,[self.TM,self.TN]),-1,1)
					self.clamped_c24=constraint_gamma*K.clip(tf.tile(self.c24,[self.TM,self.TN]),-1,1)
					self.clamped_c25=constraint_gamma*K.clip(tf.tile(self.c25,[self.TM,self.TN]),-1,1)
					self.clamped_c26=constraint_gamma*K.clip(tf.tile(self.c26,[self.TM,self.TN]),-1,1)
					self.clamped_c27=constraint_gamma*K.clip(tf.tile(self.c27,[self.TM,self.TN]),-1,1)
					self.clamped_c28=constraint_gamma*K.clip(tf.tile(self.c28,[self.TM,self.TN]),-1,1)
					self.clamped_c29=constraint_gamma*K.clip(tf.tile(self.c29,[self.TM,self.TN]),-1,1)
					self.clamped_c30=constraint_gamma*K.clip(tf.tile(self.c30,[self.TM,self.TN]),-1,1)
					self.clamped_c31=constraint_gamma*K.clip(tf.tile(self.c31,[self.TM,self.TN]),-1,1)
					self.clamped_c32=constraint_gamma*K.clip(tf.tile(self.c32,[self.TM,self.TN]),-1,1)

				else:
					self.clamped_w1 =binarize(self.w1)
	
					self.clamped_c1= constraint_gamma*binarize(tf.tile(self.c1, [self.TM,self.TN]))
					self.clamped_c2= constraint_gamma*binarize(tf.tile(self.c2, [self.TM,self.TN]))
					self.clamped_c3= constraint_gamma*binarize(tf.tile(self.c3, [self.TM,self.TN]))
					self.clamped_c4= constraint_gamma*binarize(tf.tile(self.c4, [self.TM,self.TN]))
					self.clamped_c5= constraint_gamma*binarize(tf.tile(self.c5, [self.TM,self.TN]))
					self.clamped_c6= constraint_gamma*binarize(tf.tile(self.c6, [self.TM,self.TN]))
					self.clamped_c7= constraint_gamma*binarize(tf.tile(self.c7, [self.TM,self.TN]))
					self.clamped_c8= constraint_gamma*binarize(tf.tile(self.c8, [self.TM,self.TN]))
					self.clamped_c9= constraint_gamma*binarize(tf.tile(self.c9, [self.TM,self.TN]))
					self.clamped_c10=constraint_gamma*binarize(tf.tile(self.c10,[self.TM,self.TN]))
					self.clamped_c11=constraint_gamma*binarize(tf.tile(self.c11,[self.TM,self.TN]))
					self.clamped_c12=constraint_gamma*binarize(tf.tile(self.c12,[self.TM,self.TN]))
					self.clamped_c13=constraint_gamma*binarize(tf.tile(self.c13,[self.TM,self.TN]))
					self.clamped_c14=constraint_gamma*binarize(tf.tile(self.c14,[self.TM,self.TN]))
					self.clamped_c15=constraint_gamma*binarize(tf.tile(self.c15,[self.TM,self.TN]))
					self.clamped_c16=constraint_gamma*binarize(tf.tile(self.c16,[self.TM,self.TN]))
					self.clamped_c17=constraint_gamma*binarize(tf.tile(self.c17,[self.TM,self.TN]))
					self.clamped_c18=constraint_gamma*binarize(tf.tile(self.c18,[self.TM,self.TN]))
					self.clamped_c19=constraint_gamma*binarize(tf.tile(self.c19,[self.TM,self.TN]))
					self.clamped_c20=constraint_gamma*binarize(tf.tile(self.c20,[self.TM,self.TN]))
					self.clamped_c21=constraint_gamma*binarize(tf.tile(self.c21,[self.TM,self.TN]))
					self.clamped_c22=constraint_gamma*binarize(tf.tile(self.c22,[self.TM,self.TN]))
					self.clamped_c23=constraint_gamma*binarize(tf.tile(self.c23,[self.TM,self.TN]))
					self.clamped_c24=constraint_gamma*binarize(tf.tile(self.c24,[self.TM,self.TN]))
					self.clamped_c25=constraint_gamma*binarize(tf.tile(self.c25,[self.TM,self.TN]))
					self.clamped_c26=constraint_gamma*binarize(tf.tile(self.c26,[self.TM,self.TN]))
					self.clamped_c27=constraint_gamma*binarize(tf.tile(self.c27,[self.TM,self.TN]))
					self.clamped_c28=constraint_gamma*binarize(tf.tile(self.c28,[self.TM,self.TN]))
					self.clamped_c29=constraint_gamma*binarize(tf.tile(self.c29,[self.TM,self.TN]))
					self.clamped_c30=constraint_gamma*binarize(tf.tile(self.c30,[self.TM,self.TN]))
					self.clamped_c31=constraint_gamma*binarize(tf.tile(self.c31,[self.TM,self.TN]))
					self.clamped_c32=constraint_gamma*binarize(tf.tile(self.c32,[self.TM,self.TN]))

				# Special hack for randomising the subsequent input connections: tensorflow does not support advanced matrix indexing
				shuf_x=tf.transpose(x, perm=[2, 0, 1])
				shuf_x_0 = tf.gather_nd(shuf_x, tf.cast(self.rand_map_exp_0, tf.int32))
				shuf_x_0=tf.transpose(shuf_x_0, perm=[1, 2, 0])
				
				shuf_x_1 = tf.gather_nd(shuf_x, tf.cast(self.rand_map_exp_1, tf.int32))
				shuf_x_1=tf.transpose(shuf_x_1, perm=[1, 2, 0])

				shuf_x_2 = tf.gather_nd(shuf_x, tf.cast(self.rand_map_exp_2, tf.int32))
				shuf_x_2=tf.transpose(shuf_x_2, perm=[1, 2, 0])
			
				x_pos=(1+binarize(x))/2*abs(x)
				x_neg=(1-binarize(x))/2*abs(x)
				xs0_pos=(1+binarize(shuf_x_0))/2#*abs(shuf_x_0)
				xs0_neg=(1-binarize(shuf_x_0))/2#*abs(shuf_x_0)
				xs1_pos=(1+binarize(shuf_x_1))/2#*abs(shuf_x_1)
				xs1_neg=(1-binarize(shuf_x_1))/2#*abs(shuf_x_1)
				xs2_pos=(1+binarize(shuf_x_2))/2#*abs(shuf_x_2)
				xs2_neg=(1-binarize(shuf_x_2))/2#*abs(shuf_x_2)

				ws0_pos=(1+binarize(self.clamped_w1))/2
				ws0_neg=(1-binarize(self.clamped_w1))/2

				self.out=         K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c1 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c2 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c3 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c4 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c5 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c6 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c7 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c8 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c9 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c10*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c11*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c12*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c13*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c14*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c15*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c16*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c17*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c18*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c19*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c20*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c21*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c22*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c23*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_pos[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c24*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c25*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_pos[0,:,:],self.clamped_c26*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c27*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_pos[0,:,:]*xs2_neg[0,:,:],self.clamped_c28*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c29*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_pos[0,:,:],self.clamped_c30*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c31*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[0,:,:]*xs0_neg[0,:,:]*xs1_neg[0,:,:]*xs2_neg[0,:,:],self.clamped_c32*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c1 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c2 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c3 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c4 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c5 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c6 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c7 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c8 *ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c9 *ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c10*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c11*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c12*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c13*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c14*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c15*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_pos[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c16*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c17*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c18*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c19*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c20*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c21*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c22*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c23*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_pos[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c24*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c25*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_pos[1,:,:],self.clamped_c26*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c27*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_pos[1,:,:]*xs2_neg[1,:,:],self.clamped_c28*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c29*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_pos[1,:,:],self.clamped_c30*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c31*ws0_pos*tf.tile(self.pruning_mask,[self.TM,self.TN]))
				self.out=self.out+K.dot(x_neg[1,:,:]*xs0_neg[1,:,:]*xs1_neg[1,:,:]*xs2_neg[1,:,:],self.clamped_c32*ws0_neg*tf.tile(self.pruning_mask,[self.TM,self.TN]))

			else:
				x_expanded=0
				if self.BINARY==False:
					self.clamped_w=constraint_gamma*K.clip(self.w,-1,1)
				else:
					self.clamped_w=constraint_gamma*binarize(self.w)
				for l in range(self.levels):
					x_expanded=x_expanded+x[l,:,:]
				self.out=K.dot(x_expanded,self.clamped_w*tf.tile(self.pruning_mask,[self.TM,self.TN]))
		return self.out
	def  get_output_shape_for(self,input_shape):
		return (input_shape[0], self.n_out)
	def compute_output_shape(self,input_shape):
		return (input_shape[0], self.n_out)

class my_flat(Layer):
	def __init__(self,**kwargs):
		super(my_flat,self).__init__(**kwargs)
	def build(self, input_shape):
		return

	def call(self, x, mask=None):
		self.out=tf.reshape(x,[-1,np.prod(x.get_shape().as_list()[1:])])
		return self.out
	def  compute_output_shape(self,input_shape):
		shpe=(input_shape[0],int(np.prod(input_shape[1:])))
		return shpe
