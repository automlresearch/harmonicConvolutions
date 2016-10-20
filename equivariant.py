'''Equivariant tests'''

import os
import sys
import time

import cv2
import numpy as np
import scipy.linalg as scilin
import scipy.ndimage.interpolation as sciint
import tensorflow as tf

import input_data

from steer_conv import *

from matplotlib import pyplot as plt

##### MODELS #####
	
def conv_so2(x, drop_prob, n_filters, n_rows, n_cols, n_channels, size_after_conv, n_classes, bs, phase_train, std_mult):
	"""The conv_so2 architecture, scatters first through an equi_real_conv
	followed by phase-pooling then summation and a nonlinearity. Current
	test time score is 92.97+/-0.06% for 3 layers deep, 15 filters"""
	# Sure layers weight & bias
	order = 3
	nf = n_filters
	n_params_into_fc = nf * size_after_conv
	weights = {
		'w1' : get_weights_dict([[6,],[5,],[5,]], n_channels, nf, std_mult=std_mult, name='W1'),
		'w2' : get_weights_dict([[6,],[5,],[5,]], nf, nf, std_mult=std_mult, name='W2'),
		'w3' : get_weights_dict([[6,],[5,],[5,]], nf, nf, std_mult=std_mult, name='W3'),
		'w4' : get_weights_dict([[6,]], nf, nf, std_mult=std_mult, name='W4'),
		'out0' : get_weights([n_params_into_fc, 500], name='out0'),
		'out1': get_weights([500, n_classes], name='out1')
	}
	
	biases = {
		'b4' : tf.Variable(tf.constant(1e-2, shape=[nf]), name='b3'),
		'out0' : tf.Variable(tf.constant(1e-2, shape=[500]), name='out0'),
		'out1': tf.Variable(tf.constant(1e-2, shape=[n_classes]), name='out1')
	}
	# Reshape input picture
	#print(x.shape)
	x = tf.reshape(x, shape=[bs, n_rows, n_cols, n_channels])
	
	# Convolutional Layers
	# LAYER 1
	cv1 = real_input_conv(x, weights['w1'], filter_size=5, padding='SAME')
	cv1 = complex_batch_norm(cv1, tf.nn.relu, phase_train)
	
	# LAYER 2
	cv2 = complex_input_conv(cv1, weights['w2'], filter_size=5,
							 output_orders=[0,1], padding='SAME')
	cv2 = complex_batch_norm(cv2, tf.nn.relu, phase_train)
	
	# LAYER 3---for dim reduction do striding, max-pooling interferes with
	# rotational equivariance, so is not supported.
	cv3 = complex_input_conv(cv2, weights['w3'], filter_size=5,
							 strides=(1,2,2,1), output_orders=[0,1],
							 padding='SAME')
	cv3 = complex_batch_norm(cv3, tf.nn.relu, phase_train)
	
	# LAYER 3
	cv4 = complex_input_conv(cv3, weights['w4'], filter_size=5, padding='SAME')
	cv4 = sum_magnitudes(cv4)
	cv4 = tf.nn.relu(tf.nn.bias_add(cv4, biases['b4']))
	cv4 = maxpool2d(cv4, k=2)

	# Fully-connected layers
	fc = tf.reshape(tf.nn.dropout(cv4, drop_prob), [bs, weights['out0'].get_shape().as_list()[0]])
	fc = tf.nn.bias_add(tf.matmul(fc, weights['out0']), biases['out0'])
	fc = tf.nn.relu(fc)
	fc = tf.nn.dropout(fc, drop_prob)
	
	# Output, class prediction
	return tf.nn.bias_add(tf.matmul(fc, weights['out1']), biases['out1'])

##### CUSTOM BLOCKS FOR MODEL #####
def conv2d(X, V, b=None, strides=(1,1,1,1), padding='VALID', name='conv2d'):
    """conv2d wrapper. Supply input X, weights V and optional bias"""
    VX = tf.nn.conv2d(X, V, strides=strides, padding=padding, name=name+'_')
    if b is not None:
        VX = tf.nn.bias_add(VX, b)
    return VX

def maxpool2d(X, k=2):
    """Tied max pool. k is the stride and pool size"""
    return tf.nn.max_pool(X, ksize=[1,k,k,1], strides=[1,k,k,1], padding='VALID')

def get_weights_dict(comp_shape, in_shape, out_shape, std_mult=0.4, name='W'):
	"""Return a dict of weights for use with real_input_equi_conv. comp_shape is
	a list of the number of elements per Fourier base. For 3x3 weights use
	[3,2,2,2]. I currently assume order increasing from 0.
	"""
	weights_dict = {}
	for i, cs in enumerate(comp_shape):
		shape = cs + [in_shape,out_shape]
		weights_dict[i] = get_weights(shape, std_mult=std_mult, name=name+'_'+str(i))
	return weights_dict

def get_bias_dict(n_filters, order, name='b'):
	"""Return a dict of biases"""
	bias_dict = {}
	for i in xrange(order+1):
		bias = tf.Variable(tf.constant(1e-2, shape=[n_filters]), name=name+'_'+str(i))
		bias_dict[i] = bias
	return bias_dict

##### CUSTOM FUNCTIONS FOR MAIN SCRIPT #####
def minibatcher(inputs, targets, batch_size, shuffle=False):
	"""Input and target are minibatched. Returns a generator"""
	assert len(inputs) == len(targets)
	if shuffle:
		indices = np.arange(len(inputs))
		np.random.shuffle(indices)
	for start_idx in range(0, len(inputs) - batch_size + 1, batch_size):
		if shuffle:
			excerpt = indices[start_idx:start_idx + batch_size]
		else:
			excerpt = slice(start_idx, start_idx + batch_size)
		yield inputs[excerpt], targets[excerpt]

def save_model(saver, saveDir, sess):
	"""Save a model checkpoint"""
	save_path = saver.save(sess, saveDir + "checkpoints/model.ckpt")
	print("Model saved in file: %s" % save_path)

def rotate_feature_maps(X, n_angles):
	"""Rotate feature maps"""
	X = np.reshape(X, [28,28])
	X_ = []
	for angle in np.linspace(0, 360, num=n_angles):
		X_.append(sciint.rotate(X, angle, reshape=False))
	X_ = np.stack(X_, axis=0)
	X_ = np.reshape(X_, [-1,784])
	return X_


##### MAIN SCRIPT #####
def run(model='conv_so2', lr=1e-2, batch_size=250, n_epochs=500, n_filters=30,
		bn_config=[False, False], trial_num='N', combine_train_val=False, std_mult=0.4, tf_device='/gpu:0', experimentIdx = 0):
	tf.reset_default_graph()
	if experimentIdx == 0: #MNIST
		print("MNIST")
		# Load dataset
		train = np.load('/home/sgarbin/data/mnist_rotation_new/rotated_train.npz')
		valid = np.load('/home/sgarbin/data/mnist_rotation_new/rotated_valid.npz')
		test = np.load('/home/sgarbin/data/mnist_rotation_new/rotated_test.npz')
		trainx, trainy = train['x'], train['y']
		validx, validy = valid['x'], valid['y']
		testx, testy = test['x'], test['y']

		n_rows = 28
		n_cols = 28
		n_channels = 1
		n_input = n_rows * n_cols * n_channels
		n_classes = 10 				# MNIST total classes (0-9 digits)
		size_after_conv = 7 * 7
	elif experimentIdx == 1: #CIFAR10
		print("CIFAR10")
		# Load dataset
		trainx = np.load('/home/sgarbin/data/cifar_numpy/trainX.npy')
		trainy = np.load('/home/sgarbin/data/cifar_numpy/trainY.npy')
		
		validx = np.load('/home/sgarbin/data/cifar_numpy/validX.npy')
		validy = np.load('/home/sgarbin/data/cifar_numpy/validY.npy')

		testx = np.load('/home/sgarbin/data/cifar_numpy/testX.npy')
		testy = np.load('/home/sgarbin/data/cifar_numpy/testY.npy')

		n_rows = 32
		n_cols = 32
		n_channels = 3
		n_input = n_rows * n_cols * n_channels
		n_classes = 10 
		size_after_conv = 8 * 8

	# Parameters
	lr = lr
	batch_size = batch_size
	n_epochs = n_epochs
	save_step = 100		# Not used yet
	model = model
	
	# Network Parameters
	dropout = 0.75 				# Dropout, probability to keep units
	n_filters = n_filters
	dataset_size = 10000
	
	# tf Graph input
	with tf.device(tf_device):
		x = tf.placeholder(tf.float32, [batch_size, n_input])
		y = tf.placeholder(tf.int64, [batch_size])
		learning_rate = tf.placeholder(tf.float32)
		keep_prob = tf.placeholder(tf.float32)
		phase_train = tf.placeholder(tf.bool)
		
		# Construct model
		if model == 'conv_so2':
			pred = conv_so2(x, keep_prob, n_filters, n_rows, n_cols, n_channels, size_after_conv, n_classes, batch_size, phase_train, std_mult)
		else:
			print('Model unrecognized')
			sys.exit(1)
		print('Using model: %s' % (model,))

		# Define loss and optimizer
		cost = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(pred, y))
		optimizer = tf.train.MomentumOptimizer(learning_rate=learning_rate, momentum=0.95).minimize(cost)
		
		# Evaluate model
		correct_pred = tf.equal(tf.argmax(pred, 1), y)
		accuracy = tf.reduce_mean(tf.cast(correct_pred, tf.float32))
				
		# Initializing the variables
		init = tf.initialize_all_variables()
		
	if combine_train_val:
		trainx = np.vstack([trainx, validx])
		trainy = np.hstack([trainy, validy])

	# Summary writers
	acc_ph = tf.placeholder(tf.float32, [], name='acc_')
	acc_op = tf.scalar_summary("Validation Accuracy", acc_ph)
	cost_ph = tf.placeholder(tf.float32, [], name='cost_')
	cost_op = tf.scalar_summary("Training Cost", cost_ph)
	lr_ph = tf.placeholder(tf.float32, [], name='lr_')
	lr_op = tf.scalar_summary("Learning Rate", lr_ph)
	config = tf.ConfigProto()
	config.gpu_options.allow_growth = True
	config.log_device_placement = False
	sess = tf.Session(config=config)
	summary = tf.train.SummaryWriter('logs/', sess.graph)
	
	# Launch the graph
	sess.run(init)
	saver = tf.train.Saver()
	epoch = 0
	start = time.time()
	# Keep training until reach max iterations
	while epoch < n_epochs:
		generator = minibatcher(trainx, trainy, batch_size, shuffle=True)
		cost_total = 0.
		acc_total = 0.
		vacc_total = 0.
		for i, batch in enumerate(generator):
			batch_x, batch_y = batch
			batch_x = np.reshape(batch_x, (-1, n_input)) 
			lr_current = lr/np.sqrt(1.+epoch*(float(batch_size) / dataset_size))
			
			# Optimize
			feed_dict = {x: batch_x, y: batch_y, keep_prob: dropout,
						 learning_rate : lr_current, phase_train : True}
			__, cost_, acc_ = sess.run([optimizer, cost, accuracy], feed_dict=feed_dict)
			cost_total += cost_
			acc_total += acc_
		cost_total /=(i+1.)
		acc_total /=(i+1.)
		
		if not combine_train_val:
			val_generator = minibatcher(validx, validy, batch_size, shuffle=False)
			for i, batch in enumerate(val_generator):
				batch_x, batch_y = batch
				batch_x = np.reshape(batch_x, (-1, n_input))

				# Calculate batch loss and accuracy
				feed_dict = {x: batch_x, y: batch_y, keep_prob: 1., phase_train : False}
				vacc_ = sess.run(accuracy, feed_dict=feed_dict)
				vacc_total += vacc_
			vacc_total = vacc_total/(i+1.)
		
		feed_dict={cost_ph : cost_total, acc_ph : vacc_total, lr_ph : lr_current}
		summaries = sess.run([cost_op, acc_op, lr_op], feed_dict=feed_dict)
		summary.add_summary(summaries[0], epoch)
		summary.add_summary(summaries[1], epoch)
		summary.add_summary(summaries[2], epoch)

		print "[" + str(trial_num),str(epoch) + \
			"], Minibatch Loss: " + \
			"{:.6f}".format(cost_total) + ", Train Acc: " + \
			"{:.5f}".format(acc_total) + ", Time: " + \
			"{:.5f}".format(time.time()-start) + ", Val acc: " + \
			"{:.5f}".format(vacc_total)
		epoch += 1
		
		if (epoch) % 50 == 0:
			save_model(saver, './', sess)
	
	print "Testing"
	
	# Test accuracy
	tacc_total = 0.
	test_generator = minibatcher(testx, testy, batch_size, shuffle=False)
	for i, batch in enumerate(test_generator):
		batch_x, batch_y = batch
		batch_x = np.reshape(batch_x, (-1, n_input))

		feed_dict={x: batch_x, y: batch_y, keep_prob: 1., phase_train : False}
		tacc = sess.run(accuracy, feed_dict=feed_dict)
		tacc_total += tacc
	tacc_total = tacc_total/(i+1.)
	print('Test accuracy: %f' % (tacc_total,))
	save_model(saver, './', sess)
	sess.close()
	return tacc_total



if __name__ == '__main__':
	run(model='conv_so2', lr=1e-3, batch_size=100, n_epochs=500, std_mult=0.4,
		n_filters=10, combine_train_val=False)
	#view_feature_map(20)
	#view_filters()
